#!/usr/bin/env python3
"""codex-sub-agent —— 把 `codex exec` 的实测约束编译成硬约束的包装器。

调用方只给任务信息（干什么／在哪干／多难），命令组装、隔离、存活判定、
成败判据、续跑、停止全部由本文件保证。约束写在代码里而不是文档里，
是因为文档只能靠调用方记住，而记不住的代价在 SKILL.md 的历史里写满了。
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
from typing import NamedTuple

# 护栏拒绝走独立退出码。裸 `raise SystemExit("人话")` 的退出码是 1，和
# EXIT["failed"] 撞码——调用方就分不清「--dir 写错了」和「codex 真的失败了」。
# 用 2 是因为它已经是 argparse 的参数错误码：参数写错和被护栏拒绝本来就是一类事。
USAGE_ERROR = 2


class Rejected(SystemExit):
    """护栏拒绝。把「人话」和「退出码」绑在一起，让人不可能只写对一半。"""

    def __init__(self, message):
        self.message = message
        super().__init__(USAGE_ERROR)


def reject(message):
    raise Rejected(message)


# 任务名会被直接拼成三个文件名（tasks/<名>.json、reports/<名>.md、logs/<名>.log），
# 奇怪字符会当场咬人：
#   `a/b`   写不出文件（裸 FileNotFoundError）
#   `../x`  写到 tasks/ 外面去
# 它还会出现在 codex 的 argv 和日志分隔符里，保持成简单标识符，人和 grep 都好认。
#
# 历史注记：这条限制最初还有第三个理由——反查当时用 `pgrep -f <报告路径>`，
# 而那是**正则**，`.` 和 `|` 都是元字符。2026-09-19 实测任务名 `a` 的
# `…/reports/a.md` 命中了任务 `aXmd` 的进程（`a`+任意字符+`md`），
# `stop a` 会把 SIGINT 发到 aXmd 的 codex 上。
# 那个理由现在没了——反查改成扫 /proc 做 argv 元素精确比对（见 find_codex_pid），
# 正则语义一点都不引入。但上面两条（文件名）依然成立，所以限制保留。
#
# 放在 argparse 的 type= 上，四个子命令一个都绕不过去。
_TASK_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def task_name(value):
    if not _TASK_NAME.match(value):
        raise argparse.ArgumentTypeError(
            "只允许字母、数字、点、下划线、连字符（任务名会直接当文件名用）")
    return value


# 控制字符（C0 全段 + DEL）。制表符和换行只是其中最容易撞上的两个。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _reject_control_chars(flag, value, why):
    """控制字符必须在**入口**挡住，不能指望「一般没人这么干」。

    2026-09-19 实测：给一个含制表符和换行的路径，`mkdir`／`resolve()`／
    `is_dir()` **全都放行**——文件系统这一层根本不管。

    **`why` 由调用方传，不在这里写死。** 两个调用方坏的不是同一件事：`--dir`
    进 status 的数据行、不进白名单；`--skill` 进白名单、**不**进 status 的列
    （skills 刻意不进列，见 new_meta）。合成一句「要进数据行和白名单行」就是
    对两个调用方各说了一半假话，而 stderr 那一行是 agent 唯一的线索。
    """
    hit = _CONTROL_CHARS.search(value)
    if hit:
        # 这里用 !r：value 已经确定含控制字符，裸插进错误信息会把 stderr 也弄成
        # 多行／带制表符的一坨。skill_path 后面三条的 value 是干净路径，用裸的。
        raise argparse.ArgumentTypeError(
            f"{flag} {value!r} 含控制字符 {hit.group()!r}（第 {hit.start()} 个字符）。{why}")


def work_dir(value):
    """`--dir` 的 type=。只管控制字符；「是不是目录」归 cmd_run——它要先 expanduser／resolve。

    **和 cmd_run 里 resolve 之后那道不是重复。** 这一道守的是命令行上那个原始串，
    还买到两样别的：`..` 把控制字符折叠掉的路径（`/a/x\tb/../SKILL.md` 这种，
    resolve 之后就没有控制字符了，而**原始串会原样出现在 stderr 和日志里**），
    以及 argparse 免费的错误格式（`argument --dir: …`，点名是哪个参数）。
    那一道守的是 resolve 之后的真身——软链一跨，这一道就够不着了。
    """
    _reject_control_chars(
        "--dir", value,
        "它要原样进 status 的数据行，一个换行就让「一行一任务」不成立，"
        "任何切分方案都救不回来（见 status_row）。")
    return value


def skill_path(value):
    """`--skill` 的 type=。四条缺一不可，全部当场拒，绝不「尽力而为」地继续。

    收的是 **SKILL.md 文件本身**，不是 skill 目录。

    为什么非得当场拒：2026-09-19 真跑（`--effort low`，brief 指向一个不存在的
    SKILL.md）——codex 第一步 `cat` 退 1，第二步拿 `find` 翻真实 home **跑了
    34.9 秒**，结论「未找到该文件，因此无法严格按其流程执行，尚未创建 out.txt」，
    磁盘上产物**不存在**，而本工具判 `success`、**退出码 0**。
    「零工作量」被报成「完成」，且没有任何别的信号救得回来：`cat` 的失败是 shell
    退出码，不匹配 runtime_error_lines 的三种错误形式，judge 结构上看不见它。
    """
    _reject_control_chars(
        "--skill", value,
        "它要逐行进兜底句的白名单，一个换行就把一条静默劈成两条，"
        "codex 读到的是两个都不存在的路径（见 build_skill_guard）。")
    q = pathlib.Path(value)
    if not q.is_absolute():
        raise argparse.ArgumentTypeError(
            f"--skill {value} 不是绝对路径。codex 的 cwd 是 --dir，相对路径解释不出你的意思。")
    if not q.is_file():
        raise argparse.ArgumentTypeError(
            f"--skill {value} 不是文件。要传的是 SKILL.md **文件本身**，不是 skill 目录。")
    # 第三条是审查实测逼出来的：**权限 000 的文件 is_file() 返回 True**，
    # 而 codex 的 `cat` 退 1。只查前两条的话，这次改动的核心承诺（路径写错从
    # 静默失效变当场报错）在「文件存在但读不了」这一支上原样漏掉。
    if not os.access(q, os.R_OK):
        raise argparse.ArgumentTypeError(
            f"--skill {value} 存在但当前用户读不了（权限 {oct(q.stat().st_mode)[-3:]}）。"
            f"codex 的 cat 会退 1，而那个失败判据看不见。")
    # **刻意返回原样，不 resolve()**：这个串会逐字进 brief 的白名单行，而
    # 「codex 到底读没读」就是靠日志里那一行 `cat <这个串>` 看出来的
    # （见 _add_prompt_round_args 的可观测性论证）。规范化之后，命令行上写的、
    # brief 里印的、日志里出现的就成了三个不同的串，人和 grep 都对不上。
    # 代价是审计侧：`/a/../a/SKILL.md` 和 `/a/SKILL.md` 在元数据里记成两个值。
    # 接受这个代价——元数据记的本来就是「最后一次调用给了什么」。
    return value


# 判据**刻意不含 `You've` 那一截**。codex 输出的是弯引号 U+2019，源码里写的是 ASCII
# 直引号——2026-09-22 之前两者对不上，这条特判死了整整一个版本，status 把撞上限的
# 任务全报成「报告缺失或为空」。修法不是把直引号换成弯引号（那仍然押在一个会变的
# 字符上），而是**把判据缩短到不含任何标点的那一段**：剩下全是字母和空格。
USAGE_LIMIT_MARK = "hit your usage limit"
THREAD_LOCK_MARK = "already has an active writer"
REPORT_PREVIEW_LINES = 5

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

ROUND_MARK = "===== codex-sub-agent "   # 每轮开跑前写进日志的分隔符前缀

# 本轮被信号打断时留在日志里的痕迹。刻意不以 ROUND_MARK 开头——不过这条现在
# 不再是靠人记住的前缀约定了：两个识别器都整行匹配（见下），互相不可能命中。
INTERRUPT_MARK = "----- codex-sub-agent 本轮被 INT 打断，上下文保留，可 resume -----"


# `cause` 和 `kind` 必须是**枚举**，不是自由字符串。
# docstring 里写「∈ {…}」挡不住任何东西——散文不是约束（铁律 2）。
# 两条实测后果，都是静默说谎而不是崩：
#   cause="a\nb"                 痕迹被换行劈成两行 → _MARK_LINE 整行认不出
#                                → judge 从 interrupted(130) 退回 failed(1)，
#                                  这个分支存在的理由当场复活
#   kind="interrupt and resume"  _ROUND_LINE 整行认不出 → 两轮被并成一轮，
#                                上一轮的 `Error:` 算进本轮
# _ROUND_LINE 那段注释说「三段都不含空格」：任务名由 _TASK_NAME 管着、
# 时间戳由 isoformat 结构保证，**只有 kind 一直是空头支票**，这里把它兑现。
#
# 校验用 ValueError 而不是 reject：写错的是**内部调用方**，不是用户。
# reject 走退出码 2（「参数写错或被护栏拒绝」）且只打一行人话，用在这里会让
# 那个对外契约说假话，还把一个程序 bug 伪装成用户输入问题。
# 也不用 assert：`python3 -O` 会把它整个抹掉，而这两条是承重的。
_CAUSES = ("stop", "interrupt-and-resume", "外部信号转发")
_KINDS = ("run", "resume", "interrupt-and-resume")


def _require_enum(value, allowed, name):
    """内部调用方传错枚举就当场炸，不静默、不可被 -O 关掉。"""
    if value not in allowed:
        raise ValueError(f"{name} 必须是 {allowed} 之一，收到 {value!r}")


def round_separator(kind, task, when_iso):
    return f"{ROUND_MARK}{kind} {task} {when_iso} ====="


# 分隔符与痕迹一律**整行**匹配，不是子串。
#
# 子串搜索在这个仓库里是真会说谎的：模块 docstring 自己写着「日志里还混着
# brief 原文和 codex 转述的子进程输出」，而这里的日常就是派 codex 来改
# codex_sub_agent.py 自己。源码里那两行常量定义一旦被转述进日志，**两个方向都翻车**：
#   日志里出现 `ROUND_MARK = "===== codex-sub-agent "` 这行源码
#     → 本轮文本在那里被切断（实测切剩 'ROUND_MARK = "' 共 14 个字符）
#     → 打断标记被甩到本轮之外 → judge 从 interrupted 翻成 **failed**
#   日志里出现 `INTERRUPT_MARK = "----- codex-sub-agent 本轮被 INT 打断…"` 这行源码
#     → 一轮真正失败的运行被判成 interrupted、退出码 130
#     → 照契约做决定的 agent 去 resume 一个**根本没被打断**的失败轮
# 两条都有回归测试钉着（TestRoundBoundary 里那两条，直接拿源码行当样本）。
#
# `\S+ \S+ \S+ =====$` 是精确的：round_separator 产出的 kind／task／when_iso
# 三段都不含空格——kind 是枚举、任务名字符集（_TASK_NAME）排除空格、
# isoformat(timespec="seconds") 也没有空格。
#
# **整行匹配自带一条新前提：这两行必须落在行首。** 它由**写者**保证，不是读者猜
# ——两个写入点（interrupt_codex 留痕、run_codex 写分隔符）各自在前面补换行。
# 前提不成立是常态而非边角：日志末尾由 _tee_until_exit 的 log.write(chunk) 留下，
# 而 read1(1024) 的边界是任意的，codex 流式输出被 INT 截在半行很常见。
# 这条前提要是塌了，judge 会从 interrupted(130) 退回 failed(1)——
# 正是这整轮改动要消灭的那个 bug 从第三层绕回来。
_ROUND_LINE = re.compile(r"^" + re.escape(ROUND_MARK) + r"\S+ \S+ \S+ =====$", re.M)

# 痕迹行尾可以跟一个 ` [来源]`，见 interrupt_codex 的 cause。
_MARK_LINE = re.compile(r"^" + re.escape(INTERRUPT_MARK) + r"(\s\[.*\])?$", re.M)


def has_interrupt_mark(text):
    """本轮是否真的被打断。整行匹配，不是子串——理由见 _ROUND_LINE 上面那段。"""
    return _MARK_LINE.search(text) is not None


def interrupt_codex(pid, log_path, cause):
    """发 SIGINT 并在日志留痕。这两件事必须一起发生，所以焊在同一个函数里。

    `cause` ∈ {"stop", "interrupt-and-resume", "外部信号转发"}，**必填**，
    追在痕迹行尾。三条路的含义完全不同：前两条是有人**故意**停它；第三条在
    `run_in_background` 下**根本不该发生**——它出现就等于前台误跑被 2 分钟
    超时杀掉了。不带来源的话，日志里只剩一句「被打断了」，下一个人读不出
    「你当时用错了启动方式」。
    这是把「一条编不进去的软约定（必须用 run_in_background）被违反了」变成
    **日志里可读的诊断**。`INTERRUPT_MARK` 仍是稳定前缀，`_MARK_LINE` 的
    `(\\s\\[.*\\])?$` 把来源收掉，判据不受影响。

    拆开放就会漏，而且**已经漏过一次**：`cmd_stop` 用裸 `os.kill` 打给 codex，
    而写痕迹的函数只在包装器自己的信号处理器里被调用，于是 stop 这条路上痕迹
    永远不写。2026-09-19 实测三行互相矛盾：stop 打印「上下文保留，可 resume」、
    包装器收尾判 `failed —— 报告缺失或为空＝没正常收尾`（退出码 1）、
    日志里 INTERRUPT_MARK 计数 **0**。而 agent 看到 failed 会从头重跑，
    把保着的上下文和 token 一起扔掉。
    前台误跑被 2 分钟超时杀掉时同理：那正是「run_in_background 编不进去」
    那条缓解措施最需要说话的时刻。

    **只发 INT，永不 TERM**：SIGTERM 会让 thread 永久锁死，之后 resume 永远报
    thread-store conflict，等多久都不释放，上下文全丢。
    「INT 之后仍然可以 resume」2026-09-19 在 codex 0.154.0 上真机复验过：
    run → INT → resume 跑通，两轮 session id 完全相同、token 从 3,216 接着涨到
    3,989，追问「被打断前你成功创建了哪几个文件」它自己答得出——恢复的是语义上
    的上下文，不只是一段计费记录。
    这条是单点：start_new_session、统一转发 INT、cmd_stop、interrupt-and-resume、
    以及「前台误跑也能活」那条缓解措施，全都架在它上面。换 codex 大版本时值得重验。

    **返回值刻意不给。**「它本来就没在跑」这件事由调用方在**调用之前**用
    `find_codex_pid` 判，那才是判它的地方；给个 bool 出来只是多一个「谁检查」
    的滥用面。这里的 ProcessLookupError 只是「刚好在这一瞬退出了」——
    信号没送出去就不该留下假痕迹，所以直接返回。

    留痕**必须 O_APPEND**：换成从 0 覆盖写的话，痕迹会落在本轮 start_offset
    之前（read_round 看不到 → 判 failed，这个分支存在的理由被静默重新引入），
    同时把日志头部的分隔符和 session id 抹掉（resume 再也回不来）。
    O_APPEND + 单次 os.write：小写入在 Linux 上是原子的，不会和 tee 循环
    的缓冲写互相撕裂；也刻意不碰那个已经打开的文件对象——信号处理器随时可能插在
    它的 write 中间。写不进去就算了（吞掉 OSError）：INT 已经发出去了，保住
    codex 的上下文优先于留痕。这个降级方向正是「可观测的失效不许拖垮存活」。
    """
    # 校验必须排在 os.kill **之前**：INT 发出去收不回来，先打断再发现 cause
    # 写错，那一轮白毁——和发信号前那几道闸同一条道理。
    _require_enum(cause, _CAUSES, "cause")
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            # `os.write` 可能**短写**（磁盘满／配额耗尽，这台机器根盘本来就紧张）。
            # 返回值一丢，痕迹就被截成半截——而**半截痕迹比没有痕迹更坏**：
            # 它既骗不过 _MARK_LINE，又污染了本轮文本。
            # 实测 RLIMIT_FSIZE 逼出短写：84 字节只写进 60，judge 从 130 退回 1。
            # 下面那句 `except OSError: pass` 的降级只挡「一个字节都没写进去」，
            # 挡不住「写进去一半」，所以这里必须循环写完。
            #
            # **循环覆盖的是「暂时只吞了一部分」，不是「真的没地方写了」。**
            # 磁盘真满时内核在中途抛 EFBIG，`except OSError` 接住，日志里仍然
            # 留着半截痕迹（实测 RLIMIT_FSIZE=100：文件停在 100 字节，
            # has_interrupt_mark 认不出）。这是**刻意不再补救**的：
            #   · 判据结论上，半截痕迹和没有痕迹一样都落到 failed，不更坏
            #   · 想清干净就得 ftruncate 回原长度，而 O_APPEND 下别的进程随时
            #     可能已经追加在我们后面——截它一刀比留半行脏字严重得多
            # 一句话：盘满时这行痕迹会丢，上下文不丢，codex 照样可 resume。
            #
            # `written == 0` 要跳出去：这段跑在**信号处理器**里，挂死比留半截
            # 坏得多（codex 的 tee 循环再也收不了尾）。内核不会返回 0，
            # 但这条前提不值得用一个死循环去赌。
            #
            # **无条件前置换行**：整行匹配要求这行落在行首，而没有任何一方
            # 保证它——痕迹追在日志末尾，而日志末尾是 tee 最后一次 write 留下的，
            # `read1(1024)` 的边界是任意的：codex 流式输出被 INT 截在半行是
            # **常态，不是边角**。接在半行后面的痕迹，判据当场认不出，
            # judge 从 interrupted(130) 退回 failed(1)——正是本轮要消灭的那个 bug。
            # O_APPEND 下无法「先读末字节再决定要不要补」：读和写之间别的进程
            # 可能插入。所以无条件补。多一个空行是纯外观代价，换来「这一行一定
            # 在行首」是**事实而不是指望**。
            # **不要把这个空行优化掉**——去掉它缺陷当场复活（有回归钉着）。
            data = ("\n" + INTERRUPT_MARK + f" [{cause}]\n").encode()
            while data:
                written = os.write(fd, data)
                if not written:          # 转不出去就走人，见上面「挂死」那段
                    break
                data = data[written:]
        finally:
            os.close(fd)
    except OSError:
        pass


# codex 自己的错误有三种锚定形式（2026-09-19 对 106 份真实日志全量统计），
# 少认一种就等于判据失效：
#   A 用户层   `ERROR: Reconnecting... 2/5`                  行首是 ERROR:／WARN:   23 行
#   B tracing `<ISO 时间戳> ERROR codex_core::session: …`     行首是时间戳，带 target 92 行
#   C 顶层致命 `Error: thread/resume: … active writer`        行首是大写 Error:       5 行
# 形式 C 首字母是大写 E，`^ERROR:` 大小写敏感、匹配不到它——而它恰恰是「会话被
# 锁死」那条最该报的错。
# 日志里还混着 brief 原文和 codex 转述的子进程输出（cargo 的 error[E0599]、
# pytest 的 `E   KeyError`、markdown 的 `## Warning Signs`），
# 所以绝不能用裸 grep ERROR —— 会大面积误报。
_ERR_USER = re.compile(r"^(?:ERROR|WARN):\s+(.*)")
_ERR_TRACING = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+(?:ERROR|WARN)\s+(\S+?):\s")
_ERR_FATAL = re.compile(r"^Error:\s")          # 形式 C，一律致命，没有白名单

# 形式 B 按 module target 分类，不按自由文本——文本会变，target 不会。
# 白名单之外一律计入判据，**包括 codex_core::session\***（会话建不起来正是最该报的）。
_BENIGN_TARGETS = (
    "codex_models_manager::",                      # 模型列表刷新超时，不影响本次运行
    "codex_api::endpoint::responses_websocket",    # 连接抖动，codex 自己会重连
    "rmcp::transport::worker",
    "codex_core::tools::router",                   # apply_patch 被拒后重打成功
)
# 形式 A 的良性只有这一条，且必须按**前缀**匹配：后缀有 `1/5`~`5/5` 和
# `waiting for network` 多种，写整行字面量会漏掉其余几种。
_BENIGN_USER = ("Reconnecting...",)

_SESSION_ID = re.compile(r"session id:\s*([0-9a-f-]{36})")


def strip_ansi(text):
    """剥掉颜色码。

    主线带了 `--color never`，日志本来就是纯文本；但 **resume 不认 --color、
    codex 也没有对应的 config 键**，resume 那一轮的日志照样带 ANSI。
    所以这个函数在 resume 这条路上是承重的，删不得。
    """
    return _ANSI.sub("", text)


def read_round(log_path, start_offset):
    """读日志的 `[start_offset, 之后第一个 ROUND_MARK)` 这一段——本轮，且只有本轮。

    `start_offset` 是 `run_codex` 写完本轮分隔符之后记下的**字节**偏移，
    是 codex 还没起跑之前就拿到的事实，**后来的任何一轮都不可能把它致盲**。
    偏移**不外泄**：`run_codex` 自己拿它切好本轮文本再回传（见那里的理由）。
    这替掉了「按最后一个 ROUND_MARK 往回猜」那套：日志是多个进程共写的，
    interrupt-and-resume 一确认退出就往同一个日志追加新分隔符，而被打断那一轮的
    包装器此刻正要跑判据——谁先谁后没有任何保证，实测两者落在同一秒内。
    包装器晚一步，同一份日志上的结论就从「被 INT 打断，接着 resume」
    翻成「报告缺失＝没正常收尾」。判据成本还随日志增长（0.83MB 时 7.6ms），
    长任务上天平继续朝竞争方倾斜，而长任务正是最该打断、上下文最值钱的场景。

    **偏移是字节不是字符**，所以这里走二进制 seek 再 decode：日志里全是中文
    （brief 原文、codex 的中文输出），按字符切会整体错位，切出来的开头是半截
    字节，判据读到的「本轮」根本不是本轮。偏移永远落在分隔符那行的 `\\n` 之后，
    不会切在多字节字符中间。
    `errors="replace"`：codex 被 INT 打断时可能只写出半截字节，裸 decode 会把
    判据整个打崩。
    右端截到「起点之后的第一个分隔符**行**」，所以后一轮的内容也不会被吞进来。
    整行匹配而不是子串搜索：日志里转述到那行源码就会在那里被误切（见 _ROUND_LINE）。
    """
    if not log_path.exists():
        return ""
    with open(log_path, "rb") as f:
        f.seek(start_offset)
        raw = f.read()
    text = strip_ansi(raw.decode("utf-8", "replace"))
    m = _ROUND_LINE.search(text)
    return text if m is None else text[:m.start()]


def read_last_round(log_path):
    """最后一轮。**只给外部观察者用**（`status`）——它手里没有偏移。

    这是它诚实的上界：它本来就只能看到最后一轮，说不出更多。
    本轮的拥有者（cmd_run / _resume_round）绝不该用它：拥有者手里有事实，
    用这个就等于把事实换回推测。
    """
    if not log_path.exists():
        return ""
    text = strip_ansi(log_path.read_text(errors="replace"))
    # 整行匹配、取最后一个（理由同 read_round）
    last = None
    for m in _ROUND_LINE.finditer(text):
        last = m
    return text if last is None else text[last.start():]


def runtime_error_lines(round_text):
    """本轮日志里 codex 自己的错误行（已滤掉良性 target 与良性用户层消息）。

    **本轮的边界由调用方划好再传进来**，本函数不再自己切——切法有两种
    （拥有者用偏移、观察者用最后一轮），藏在这里面就只剩「猜」一种。

    这里仍然 strip_ansi 一道：它是幂等的，而少了它，一个直接拿原始日志文本
    调进来的人会静默拿到空结果（`^ERROR:` 匹配不到 `\\x1b[31mERROR:`）——
    静默的错比多一次正则扫描贵得多。

    刻意没有「只看末 N 行」的窗口参数。那个窗口过去偷偷承担着「运行中已经恢复
    过去的错误不算」这个语义，而这件事现在由 target 白名单正经做了，窗口只剩下
    劣化替代品的身份：留着它，下一个撞上 60 行尾部堆栈的人就会把 50 改成 500，
    然后每一次已恢复的错误都静默变成 suspect。
    """
    hits = []
    for raw in strip_ansi(round_text).splitlines():
        line = raw.strip()
        if _ERR_FATAL.match(line):
            hits.append(line)
            continue
        m = _ERR_TRACING.match(line)
        if m:
            if not m.group(1).startswith(_BENIGN_TARGETS):
                hits.append(line)
            continue
        m = _ERR_USER.match(line)
        if m and not m.group(1).startswith(_BENIGN_USER):
            hits.append(line)
    return hits


def hit_usage_limit(error_lines):
    """本轮是不是撞上了账号额度上限。

    **收的是 `runtime_error_lines()` 的返回值，不是整轮日志文本。** 这是承重的：
    日志里混着 brief 原文和 codex 转述的子进程输出（本文件 `_ERR_*` 上方那段注释
    已经为同一个理由否掉了裸 grep ERROR）。2026-09-22 全量核对 268 份现网日志：
    27 份里有 54 行真·额度错误，另有 3 份里 71 行只是**提到**这句话（brief 原文、
    源码引用）。对整轮文本做子串匹配，后面那 71 行会被判成额度问题——而在
    `cmd_run` 里，那意味着**删掉元数据、换账号重跑整个任务**。判据一旦成为
    控制依据，证据就必须来自**已经分类过的错误行**。

    **`judge` 和 `cmd_run` 共用这一个谓词。** 两处各写一套必然漂移，
    而判据漂移正是本工具反复在修的那类 bug。`cmd_run` 为此要多跑一次
    `runtime_error_lines`（实测 0.83MB 日志约 7.6ms，而 run 是分钟级的）——
    不为省这 8ms 给 `Verdict` 加字段：那要改 7 个构造点，churn 比多一次扫描大。
    """
    return any(USAGE_LIMIT_MARK in line for line in error_lines)


# 两条正则对应现网两种真实形态（见 `parse_reset_time`）。
# 分组全部具名，不用位置号：`raw` 这一组划定的就是**落盘时那个审计串**
# （只含日期时间，不含 "try again at " 这个引子），而位置号会随着日后加一个
# 括号整体漂移，漂了之后 raw 里悄悄多出半句英文，谁也不会发现。
#
# 「只有时刻」这条**紧贴在 `try again at` 之后**（不是在全文里找时刻）：
# 松开这个锚点，带日期消息里的 `5:04 PM` 会被它截胡，于是一条自带完整日期的
# 消息被当成「今天／明天的 5:04」——比解析不出更坏。
_RESET_AT_DATED = re.compile(
    r"try again at\s+"
    r"(?P<raw>"
    r"(?P<month>[A-Z][a-z]{2})\s+"              # Sep
    r"(?P<day>\d{1,2})(?:st|nd|rd|th),\s+"      # 25th,
    r"(?P<year>\d{4})\s+"                       # 2026
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*"   # 5:04
    r"(?P<half>[AP]M)"                          # PM
    r")")
_RESET_AT_TIME_ONLY = re.compile(
    r"try again at\s+"
    r"(?P<raw>(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<half>[AP]M))")
_MONTHS = {name: number for number, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def _to_24h(hour, half):
    """12 小时制换 24 小时制；小时不在 1–12 就返回 `None`。

    **不取模。** `int("99") % 12 + 12 == 15`——一条畸形消息会被静默编造成
    一个看起来合理的恢复时间，而这个值要落盘、要排序、要显示给人看。
    12 AM = 0 点、12 PM = 12 点，所以合法区间内仍然要取模。
    """
    hour = int(hour)
    if not 1 <= hour <= 12:
        return None
    return hour % 12 + (12 if half == "PM" else 0)


def parse_reset_time(text, now):
    """从撞上限的那段话里抠出恢复时间，抠不到就 `None`。

    现网两种形态（2026-09-22 对 269 份日志全量核对）：

        try again at Sep 25th, 2026 5:04 PM     34 行 / 17 份   Pro，周额度
        try again at 11:10 AM                   22 行 / 11 份   Plus，5 小时档

    第二种**只有时刻没有日期**，占真实消息的 39%。不认它的代价是：
    5 小时窗口里每次 run 都要白撞这个账号一次、白探一轮。

    歧义（11:10 是今天还是明天）只在读到消息的那一刻存在，而消息说的是
    "try again **at**"——未来。所以取 **>= now 的下一个该时刻**。
    `now` **必填**：这是本功能唯一读时钟的地方，调用方必须显式交出它用的是哪个，
    否则测试里一个隐式的 `datetime.now()` 会让「明天还是今天」随测试运行的时刻飘。
    排序（`accounts_by_availability`）**一次都不读时钟**。

    返回 `(时间, 原始串)`——两样必须来自**同一次匹配**：原始串是落盘时的审计线索
    （解析错了一眼看得出），拆成两个函数就可能各匹配各的、对不上。

    **返回朴素本地时间，不编造时区。** 消息里没有时区，实测也推不出来（恢复时间在
    3~4 天后，拿文件 mtime 反推不出来）。这样做安全，是因为这个值**只当排序键**
    （见 `accounts_by_availability`）：误差 ±12h 只在两个账号的恢复时间相差 12h
    以内时才改变顺序，代价是多一次 codex 启动。

    抠不到宁可返回 `None`——调用方会退化成「无记录＝排最前＝照样会被试到」，
    比写一个假时间进去安全得多。**带日期那一支匹配上但内容非法（月份名乱写、
    Feb 31st、小时不在 1–12）时直接 `None`，不退回「只有时刻」那一支**：
    这条消息自带日期，拿它的时刻当今天／明天就是编一个假恢复时间出来。
    """
    m = _RESET_AT_DATED.search(text)
    if m is not None:
        month = _MONTHS.get(m.group("month"))   # [A-Z][a-z]{2} 会匹配 "Foo"，必须再查表
        hour = _to_24h(m.group("hour"), m.group("half"))
        if month is None or hour is None:
            return None
        try:
            return (datetime.datetime(int(m.group("year")), month, int(m.group("day")),
                                      hour, int(m.group("minute"))),
                    m.group("raw"))
        except ValueError:              # Feb 31st 这种
            return None

    m = _RESET_AT_TIME_ONLY.search(text)
    if m is None:
        return None
    hour = _to_24h(m.group("hour"), m.group("half"))
    if hour is None:
        return None
    when = now.replace(hour=hour, minute=int(m.group("minute")), second=0, microsecond=0)
    if when < now:                      # 今天这个点已经过了，那说的就是明天
        when += datetime.timedelta(days=1)
    return when, m.group("raw")


USAGE_LIMIT_FILE = "usage_limit.json"


def usage_limit_path(home):
    """限流记录放隔离目录根部。

    限流是**账号**的属性，而账号已经有一个家，不必为它新开一个状态目录。
    `ensure_isolation` 的不变量是「五个子目录存在、共享扫描根为空、config.toml
    不是软链、auth.json 软链到该账号」——**它不枚举也不拒绝顶层多余文件**
    （2026-09-22 实测：放一个文件进去再跑一次，不拒且文件保留）。
    """
    return home / USAGE_LIMIT_FILE


def _usage_limit_tmp(home, pid):
    """写限流记录用的临时文件名。**带 pid，不能用固定名。**

    和 `write_meta` 的关键区别：那份是**任务级**的，一个任务只可能有一个写者
    （`cmd_run`/`cmd_resume` 都先拒绝「还在跑」的同名任务），所以固定名安全。
    这份是**账号级、跨任务共享**的——同一个账号上的两个任务可以同时撞上限、
    同时写。固定名会让两个写者把同一个临时文件写成混合内容，
    然后原子替换把这份混合内容装进去，`read_usage_limit` 读出 `None`。
    """
    q = usage_limit_path(home)
    return q.with_name(f"{q.name}.{pid}.tmp")


def write_usage_limit(home, reset_at, raw):
    """记下这个账号什么时候恢复。**只在 `parse_reset_time` 成功时调用。**

    `raw` 是审计线索：解析错了（时区、OpenAI 改文案）一眼看得出，不用翻日志。
    **没有 `seen_at`**——唯一的读者是排序，排序只看 `reset_at`；
    没有消费者的字段一旦加进去就再也删不掉了。

    **写不进去只提示、不抛。** 这条记录是优化不是前提：写失败最多让下次排序少
    一条依据，而抛出去会把**正在进行的换号重试打断**——一次本来还能被下一个
    账号救回来的任务就这么死了。吞在这里而不是留给调用方包 try：调用方每多
    一个就要记得包一层，而「必须记得」正是这个工具存在的理由本身。
    吞掉但**出声**——静默失效是这个仓库反复在修的那类病。
    """
    tmp = _usage_limit_tmp(home, os.getpid())
    try:
        tmp.write_text(json.dumps({"reset_at": reset_at.isoformat(), "raw": raw},
                                  ensure_ascii=False, indent=2))
        os.replace(tmp, usage_limit_path(home))
    except OSError as e:
        print(f"[codex-sub-agent] 提示：限流记录写不进 {usage_limit_path(home)}（{e}）。"
              f"不影响本轮，只是下次排账号顺序时少一条依据。")


def read_usage_limit(home):
    """这个账号预计什么时候恢复；没记录或记录坏了都返回 `None`。

    **坏了返回 `None` 是刻意的**：调用方把 `None` 当成「无记录＝排最前＝照样会被
    试到」，那是安全的一边。反过来（读不出来就认定它还在限流）会把一个可用账号
    锁死，而调用方看不出原因——那正是本工具反复在修的那类谎。

    有效的形状**只有一种**：两个键都在、`raw` 是字符串、`reset_at` 是**朴素**
    ISO 串。剩下全部当没记录——**「像是对的」那两种才是会咬人的**：

      带时区的 `reset_at`  `accounts_by_availability` 里要拿它和 `datetime.min`
                           （朴素）比，实测 `TypeError: can't compare offset-naive
                           and offset-aware datetimes`，一个手写坏的状态文件
                           把整条 auto 命令打死。而按设计它最坏只该让顺序排差
      缺 `raw` / 类型不对   不是本函数写出来的东西。审计线索没了就查不出解析
                           错在哪，而这份记录的可信度本来就全靠它
    """
    try:
        got = json.loads(usage_limit_path(home).read_text())
        if not isinstance(got["raw"], str):
            return None
        when = datetime.datetime.fromisoformat(got["reset_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return None if when.tzinfo is not None else when


def accounts_by_availability():
    """按「预计什么时候能用」给候选账号排序。

    **额度记录只排顺序，不准拒绝**——本设计最重要的一条约束。
    「这个账号真的没额度了」这个结论只能来自**真实尝试**，不能来自这里读到的
    文件。文件会错（时区、时钟偏移、OpenAI 改文案，以及日志被回显污染——
    见 `hit_usage_limit`）。只排顺序的话后果被限制在「顺序排差一格」；一旦允许
    它拒绝，同一个错就变成「把好账号误判成死的、还告诉调用方没号可用」。

    **登录态是唯一的例外，而且它不违反上面那条。** 那条约束管的是**额度记录**
    ——一份会过期、会出错的缓存。登录态是当场可查的硬前提：没有 `auth.json`，
    `ensure_isolation` 会直接 `reject` 退出 2，于是**一个账号缺登录态就会把整条
    auto 命令打死**，哪怕别的账号完全可用。所以在这里就把它们滤掉。

    排序键两段：
    ① 恢复时间；没记录就是 `datetime.min`，排最前，照样会被试到。
       **过期记录不会自动变成「无记录」**：本函数不读当前时间，一条过期记录
       永远排在无记录之后。这不是 bug——它排进「恢复得早的那一批」，
       批内按先后排，语义依然正确，而清理需要读时钟、需要定义「多久算过期」，
       是纯增实体
    ② 账号名——**第二键必须有**，否则顺序跟着 `account_choices()` 的扫描顺序漂，
       同一个输入在两台机器上给出两种结果

    **刻意没有「这个任务已经住在哪」这一键，也就不收 `task`。** v3 有过
    （住着的排最前，沿用它的家），那是为**轮内重试**服务的：先试旧家，
    撞上限再搬走。v4 去掉重试之后它变成陷阱——任务住的那个账号限流了也照样
    被选中，于是每次重跑都选它、每次都撞上限，**永远换不掉**，而 auto 的
    全部意义就是换掉它。搬家现在由 `cmd_run` 在入口处显式做一次。
    """
    def sort_key(account):
        return (read_usage_limit(isolation_home(account)) or datetime.datetime.min,
                account)
    return sorted((a for a in account_choices() if auth_source(a).exists()), key=sort_key)


def extract_session_id(log_text):
    m = _SESSION_ID.search(strip_ansi(log_text))
    return m.group(1) if m else None


def clear_report(report_path):
    """每轮开跑前删掉报告文件。

    2026-09-19 实测：codex **只在正常收尾时**写 `-o` 指定的文件，启动时**不**
    truncate。所以 run 成功写下报告、随后 resume 秒死于写锁时，判据会读到上一轮
    的旧报告并判 success——工具在说谎（真实日志里有 5 份样本走的正是这条路）。
    删掉之后本工具成为报告的唯一创建者，「报告存在」才重新是一句关于本次调用的
    真话。代价是失败的 resume 会连带毁掉上一轮的报告：可以接受，日志是追加的，
    上一轮的内容还在里面。
    """
    report_path.unlink(missing_ok=True)


class Round(NamedTuple):
    """一轮的两件产物：报告文件路径 + 本轮的日志文本。**成对，不拆开传。**

    拆开传时 `run_codex` 声称消灭掉的那类「必须记得对齐」只是上移了一层：
    它的 docstring 写着「报告路径由本函数拥有……这三条在结构上就违反不了了」，
    而调用方随后又自己算一遍同一个路径喂给 judge。更糟的是
    `judge(report, <任意 str>)` 传错文本是**静默算对**的——拿另一个任务的日志
    去判这个任务的报告，不报错，只给一个假结论。
    成对之后，「哪份报告配哪段文本」在拥有者那条路上写不错：`run_codex` 自己
    把两样一起交出来。外部观察者（status）没有本轮文本可用，只能显式构造
    `Round(report, read_last_round(log))`——显式，所以看得见它在用推测。
    """
    report_path: object   # pathlib.Path
    text: str             # 本轮的日志文本（已 strip_ansi、已切好边界）


class Verdict(NamedTuple):
    state: str   # success / running / interrupted / suspect / failed
    reason: str  # 一行人话
    detail: list # suspect／failed：出事的那几行；success：报告前几行


def judge(round):
    """唯一的成败判据——**只看产物和本轮日志**。`run`／`resume` 收尾和 `status`
    共用它，避免两处判据漂移。

    收的是一个 `Round`（报告路径 + 本轮文本）而不是两个参数：那两样必须配套，
    而拆开传时配错是**静默算对**的。理由写在 `Round` 上。

    **本轮的日志文本由调用方划好再传进来**，判据自己不划边界。划边界的两种人
    不一样：本轮的拥有者拿 `run_codex` 回传的 `Round`，外部观察者只能看最后
    一轮（`read_last_round`）。把这件事塞回 judge 里，就只剩「猜」一种做法，
    而那正是这次要修掉的整类 bug。

    **存活与否不在这里判**：那是进程的事，不是产物的事。三个调用点里有两个
    恒传 `None`，说明 pid 本来就只属于剩下那一家（`status`）。留着它还要白烧：
    `pid is not None` 那一支根本不碰 round_text，读出来直接丢，而 status 的
    热路径恰恰是轮询**还在跑**的任务（实测 read_last_round：0.83MB 约 10ms、
    8.3MB 约 100ms，不带任务名时还要乘任务数）。

    codex 的退出码不可信：中途已恢复的工具 ERROR（apply_patch 被拒后重打成功）
    也会把退出码染成 1。所以判据只看产物和日志，不看退出码。
    """
    report_path, round_text = round.report_path, round.text
    errors = runtime_error_lines(round_text)
    # errors="replace"：codex 被 SIGINT 打断时可能只写出半截字节，
    # 裸 read_text 会 UnicodeDecodeError 把判据整个打崩。
    report_text = report_path.read_text(errors="replace") if report_path.exists() else ""

    # “报告没出现＝没正常收尾”——这是 codex 写 -o 的唯一时机。
    # 前提是每轮开跑前把上一轮的报告删掉（见 clear_report），否则旧报告会被
    # 当成本轮的产物，一次失败的运行会被判成 success。
    if not report_text.strip():
        # 三种特判只改 reason、不新增状态（打断那条除外，它本来就是第五态）：
        # 补救手段不同（接着 resume／换账号／新起任务），但都属于「没正常收尾」
        # 这一种事实，状态机不该为此变复杂。
        #
        # **打断排最前。** 这个标记是本工具自己写的（interrupt_codex 发完 INT 才写），
        # 是关于「我们做了什么」的**不可伪造**证据；下面两条靠的是日志里的字面串，
        # 而日志里混着 brief 原文、codex 读文件的回显、子进程输出——2026-09-22 实测：
        # 一个完全正常的账号，因为任务内容涉及额度处理，日志里就出现了
        # `ERROR: …hit your usage limit`。
        # 「两者都真」几乎不可能：codex 撞上限会自己退出，那时没有进程可以被 INT。
        # 顺序反了的代价是不对称的：把「被打断、resume 就行」判成额度问题，
        # cmd_run 会删掉元数据、换账号把整个任务重跑一遍。
        # 日志里同时有 codex_core::session 的错误行是常态（被 INT 打断几乎必然
        # 留下 failed to record rollout items），那些照常进 detail，不改状态。
        if has_interrupt_mark(round_text):
            return Verdict("interrupted",
                           "本轮被 INT 打断，上下文保留——接着 resume 即可，不用重跑", errors)
        if hit_usage_limit(errors):
            return Verdict("failed", "撞上账号额度上限，换账号或等额度恢复", errors)
        if THREAD_LOCK_MARK in round_text:
            return Verdict("failed",
                           "会话被写锁占住（上一轮没真的结束，或曾被 SIGTERM 杀过），只能新起一个任务",
                           errors)
        return Verdict("failed", "报告缺失或为空＝没正常收尾", errors)

    if errors:
        return Verdict("suspect", f"报告在，但本轮日志有 {len(errors)} 条未分类的 codex 错误", errors)

    # 报告内容由 brief 决定（要 commit 还是要别的），属于任务层不属于工具层。
    # 只预览前几行，让调用方自己核对 brief 要的东西在不在——不解析 JSON：实测
    # 156 份真实报告只有 4 份是 JSON，`-o` 写的是 agent 的最后一条消息，通常是
    # markdown 散文。要结构化输出那是 --output-schema 的事。
    preview = [l for l in report_text.splitlines() if l.strip()][:REPORT_PREVIEW_LINES]
    # **这条 success 仍然盖不住「它压根没干活」这一整类。** `--skill` 只堵住其中
    # 一个实例（路径写错）：codex 读完 skill 之后**拒绝执行**、只在报告里写一句
    # 「我没做」，这里照样判 success——报告非空、日志无错误行，两个条件都满足。
    # 结构上看不见：产物存在性由 brief 的 DoD 定义（要 commit 还是要别的），
    # 那是任务层的事，工具层没有任何东西知道该去找什么。**另案，别在这里补。**
    return Verdict("success", "正常收尾，本轮日志无未分类错误", preview)


MODEL = "gpt-6-astra"

# 隔离目录自己的 config，绝不软链主配置。
# 2026 年踩过：`codex-acct` 把 config.toml 软链到主配置，一用就把 MCP、plugins、
# hooks、memories 全带回来，隔离当场失效。账号和隔离是正交的两件事，要组合。
#
# 这份初始内容**刻意只有注释**。model／effort／sandbox_mode／approval_policy
# 由 CLI 每次显式传，写进 config 就是同一条事实有两个家，还是个会被静默覆盖的
# 缺省值（现存两个隔离目录的 model/effort/service_tier 本来就互相打架）。
CONFIG_NOTE = '''# codex-sub-agent 的隔离配置。
# 这个文件必须是本目录自己的普通文件，不许软链 ~/.codex/config.toml——
# 软链会把主配置的 MCP／plugins／hooks／memories 全带回来，隔离当场失效。
# 刻意不写 model / model_reasoning_effort / sandbox_mode / approval_policy：
# 那些由 codex-sub-agent 每次运行显式传参，写在这里只会变成一份会被静默覆盖的缺省值。
# codex 自己会往下面追加 [projects.*] trust_level，那是它的状态，不要手动清。
'''


# 账号名里不许有空白或控制字符。它会进 status 数据行的第二列，而那一列是
# `split(maxsplit=4)` 的切分边界之一（见 status_row）。和任务名限字符集同一条
# 理由：这个值会进入按空白切分的输出，**约束必须在入口**——而扫描就是它进入
# 系统的唯一入口。刻意只拒空白和控制字符、不照搬 _TASK_NAME 的字符集：
# `工作` 这种账号名一点问题都没有，按任务名的白名单会把它一起误伤。
_BAD_IN_ACCOUNT = re.compile(r"[\s\x00-\x1f\x7f]")


AUTO = "auto"
# `default` 是主账号的固定名（不对应 ~/.codex-accounts 下的目录），
# `auto` 是 `--account` 的模式词。两个都不许被真账号目录占用。
RESERVED_ACCOUNT_NAMES = ("default", AUTO)


def account_choices():
    """账号可选项由实际目录扫描得出，不硬编码——加了账号就自动认。

    扫到坏名字就**拒跑**，不静默跳过：跳过的话这个账号的隔离目录对
    `find_meta`／`all_metas` 也一起消失，住在里面的任务从此 status 看不见、
    而同名 run 又会当它不存在——正是这次改动要消灭的那种静默失效。

    **代价要说清：拒绝半径是整个 CLI。** 本函数挂在 `build_parser()` 的
    `choices=` 上，所以一个坏目录名会让 `status`／`stop` 也退 2——**连正在跑的
    任务都停不了**，只能先把那个目录改名。选这一半是因为另一半更坏（静默失效
    没有任何信号），而这一半的修法是一条 `mv`，且 stderr 直接点名是哪个目录。
    """
    accounts_dir = pathlib.Path.home() / ".codex-accounts"
    extra = sorted(q.name for q in accounts_dir.iterdir() if q.is_dir()) if accounts_dir.is_dir() else []
    for name in extra:
        hit = _BAD_IN_ACCOUNT.search(name)
        if hit:
            reject(f"账号目录名 {accounts_dir / name} 含空白或控制字符 {hit.group()!r}"
                   f"（第 {hit.start()} 个字符）。账号名会进 status 数据行的第二列，"
                   f"那一列是按空白切分的边界。改掉这个目录名再跑。")
        # 拒跑而不是静默去重：静默去重会让住在那个目录里的任务从 status 里消失，
        # 正是本函数上面那段注释反复强调要避免的静默失效。
        if name in RESERVED_ACCOUNT_NAMES:
            reject(f"账号目录 {accounts_dir / name} 占用了保留名 {name!r}。\n"
                   f"`default` 是主账号的固定名，`{AUTO}` 是 --account 的模式词。\n"
                   f"实测后果：目录叫 default 时候选里会出现两个 default、"
                   f"两次指向同一个隔离目录，find_meta 会对同一份元数据数出两份并误拒；"
                   f"目录叫 {AUTO} 时这个账号再也没法被明确指定。\n"
                   f"把这个目录改个名。")
    return ["default"] + extra


def isolation_home(account):
    """每个账号一个隔离目录，任务的全部产物都落在这里。

    **落点在 `$HOME` 下，不在被改的那个仓库里**，这是判据不是习惯：
    `tasks/`、`reports/`、`logs/` 全在仓库外，过程文件就进不了 git，
    谁 `git add -A` 都收不到它。
    从前报告写在 `--cd` 那个仓里、靠 `tmp_codex_` 前缀加一句 brief 叮嘱来防，
    2026-09-13 破了：子代理把 380 行的 `tmp_codex_final_ban_spec_review.json`
    提交进了分支，**而它的收尾自述里写着「未提交 tmp_codex_*」**。
    软约定拦不住，换成结构上够不着才拦得住。
    """
    base = pathlib.Path.home()
    return base / ".codex-subagent" if account == "default" else base / f".codex-subagent-{account}"


def auth_source(account):
    base = pathlib.Path.home()
    return base / ".codex" / "auth.json" if account == "default" else base / ".codex-accounts" / account / "auth.json"


def shared_skill_root():
    """CODEX_HOME 管不到的共享扫描根。放了东西 codex 就看得见，隔离的前提不成立。"""
    return pathlib.Path.home() / ".agents" / "skills"


def ensure_isolation(account):
    """保证隔离目录满足全部不变量，不满足就拒跑（而不是“尽力而为”地继续）。

    **它挡的是动机，不是能力**——这条必须写明白，否则下一个人会拿它去推错结论。
    2026-09-19 实测：`collaboration.spawn_agent` 等六个工具**恒在**，
    `--disable multi_agent` **无效**（加与不加，codex 报的工具清单逐字相同）；
    `skip_host_skill_discovery` 也不影响服务端那 5 个 skill（两次清单逐字吻合）。
    真正被挡住的是**主目录那一侧**：23 个 skill、2 个 MCP、3 个 hook 全部看不见。
    服务端那 5 个与「想去编排」无关，**刻意不管**（关它们是解决不存在的问题）。
    `~/.agents/skills` 放哨兵文件确实会被 codex 列出来——所以下面那条
    「共享扫描根非空即拒跑」是**承重的**，不是防御性编程。

    顺带记下一条被审查推翻的错理由：曾经写过「不把 skill 软链进来，是因为
    codex 看见流程类 skill 就想去编排」——**对精选集不成立**（只软链
    test-driven-development 时，codex 够不着 subagent-driven-development，
    那个口子打不开）。不软链的真理由在 _add_prompt_round_args 里：可观测性 +
    奥卡姆。留着一条错理由比没有理由更危险。
    """
    d = isolation_home(account)
    # tasks/reports/logs 必须先建好：目录不存在时 codex 不会自己建，`-o` 静默
    # 写失败（log 末尾只留一行 Failed to write last message file），而判据是
    # “报告没出现＝没正常收尾”——一次成功的运行会被判成失败。2026-09-13 连踩两次。
    for sub in ("skills", "plugins", "tasks", "reports", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)

    # 拒跑而不是打印警告：隔离的前提一旦被破坏，本工具的核心承诺就是空的，
    # 而警告会被淹没在几千行 codex 输出里没人看见。
    intruders = sorted(q.name for q in shared_skill_root().iterdir()) if shared_skill_root().is_dir() else []
    if intruders:
        reject(
            f"{shared_skill_root()} 非空：{'、'.join(intruders)}\n"
            f"那是 CODEX_HOME 管不到的共享扫描根，放了东西 codex 就看得见，隔离不成立。清空它再跑。")

    config = d / "config.toml"
    # 唯一的 config 不变量是「它是普通文件」。内容既不校验也不重写：2026-09-19
    # 实测 codex 自己往这个文件里追加 [projects."…"] trust_level = "trusted"，
    # ~/.codex-subagent 已累积 19 段——校验内容则第二次 run 就失败，重写则抹掉
    # codex 自己的 trust 状态。
    if config.is_symlink():
        reject(f"{config} 是软链——隔离会失效（软链主配置会把 MCP/plugins/hooks 全带回来）。\n"
               f"请删掉它，重跑本命令会生成一份新的。")
    if not config.exists():
        config.write_text(CONFIG_NOTE)

    src = auth_source(account)
    if not src.exists():
        reject(f"账号 {account} 没有登录态（{src} 不存在）。先跑 `codex-acct login {account}`。")
    auth = d / "auth.json"
    if not (auth.is_symlink() and auth.resolve() == src.resolve()):
        if auth.exists() or auth.is_symlink():
            auth.unlink()
        auth.symlink_to(src)
    return d


# 兜底句的**词干**。派生出来的两种措辞都含有它，SKILL.md 历史上印过的那句
# 「**不得使用任何 skill，除非本 brief 明确指定。**」也含有它。
# 它只挡**复述过本工具措辞**的那一类——2026-09-20 实测：`禁止使用任何 skill。`
# 和 `不要用任何 skill，除非我说了。` 都**放行**。挡不住的那一类靠的是工具的
# 句子排在最前面（见 prepend_skill_guard 的拼接顺序），本来优先级就明确。
# 刻意**不**升级成语义匹配：那要引入一个新判据，换来的只是挡住一类优先级本来
# 就不含糊的输入。
SKILL_GUARD_STEM = "不得使用任何 skill"

# 主线和 resume 都固定带上的参数。调用方碰不到它们，也就不可能漏掉。
# `--color never` **不在这里**：resume 不认它（见 build_resume_argv）。
_COMMON = ["-c", "approval_policy=\"never\"", "-c", "project_doc_max_bytes=0",
           "--skip-git-repo-check", "--disable", "plugins"]


def _require_skill_paths(skills):
    """白名单的形状闸。照 `_require_enum` 的做法：内部调用方传错就**当场炸**。

    `args.skills` 恒为 `tuple[str]` 这个不变量**只到 parser 为止**
    （见 _AppendSkillPath），parser 之下本来一道闸都没有，而两种坏法全是静默的：

        build_skill_guard("/abs/SKILL.md")  → 逐字符拼出 14 行：`- /`、`- a`、`- b`…
        build_skill_guard(None)             → 返回「无白名单」那句，白名单被**无声吞掉**
        new_meta(..., "/abs/SKILL.md")      → 照样落盘

    仓内三个调用点都传 `args.skills`，所以今天不可达——`kind`／`cause`／`state`
    当初上 `_require_enum` 时也一样不可达。**散文不是约束。**

    只认 tuple，不认 list：不变量就是 tuple，放行 list 等于把刚统一掉的两种类型
    又放回来。每一项必须是 `str`——`Path` 在兜底句里印出来一模一样，却会让
    `write_meta` 的 `json.dumps` 在很久以后才炸；整数更坏，json 收得下，静默落盘。
    """
    if not isinstance(skills, tuple):
        raise ValueError(
            f"skills 必须是 tuple（只有一条就写 (路径,)），收到 "
            f"{type(skills).__name__}: {skills!r}")
    for q in skills:
        if not isinstance(q, str):
            raise ValueError(
                f"skills 的每一项必须是路径字符串，收到 {type(q).__name__}: {q!r}")


def build_skill_guard(skill_paths):
    """本轮的兜底句。**每轮派生，不是常量**——白名单是每一轮的事。

    旧常量那半句「除非本 brief 明确指定」本来就是「CLI 没有这个参数」的变通：
    调用方无处声明白名单，只好让 brief 正文去破例。`--skill` 出现之后那半句就
    该消失——白名单由 CLI 指定，brief 正文不再是声明渠道。

    白名单**逐行**列出，所以路径里一个换行就能把一条静默劈成两条
    （见 _reject_control_chars，那条拒绝就是为这里守的）。

    **「除外」后面那句「动手前先逐个读一遍」是承重的，不是客套。**
    只列路径的话，codex 拿到的是**许可**（你可以用这几个），而不是**指令**
    （去读）。不读 → 日志里就没有那行 `cat <路径>` → `judge` 结构上看不见 →
    照报 success，正是本次立项要杀的那类失效（34.9 秒那次）换了个形状。
    而「刻意不把 skill 软链进隔离目录」的全部论证都架在那行 `cat` 上
    （见 _add_prompt_round_args 的可观测性那段）——指令没了，那条论证也一起塌。
    无白名单那一支**刻意不带**这句：没东西可读，加上去只是句废话。
    """
    _require_skill_paths(skill_paths)
    if not skill_paths:
        return f"**{SKILL_GUARD_STEM}。**"
    return (f"**{SKILL_GUARD_STEM}，以下几个除外（动手前先逐个读一遍）：**\n"
            + "\n".join(f"- {q}" for q in skill_paths))


def check_brief_has_no_guard(brief_text):
    """**调用方自己写了兜底句就拒跑**，不去重、不合并。**只拒绝，无副作用。**

    两句兜底句并存时的优先级根本不该需要被定义（铁律 2）。而 SKILL.md 把那句话
    明文印过、调用方照抄进 brief 开头是**可达路径**。拒绝比去重少一个分支，
    且把「无定义」变成「不可能」。

    抽成独立函数，是因为它必须在**两个**地方跑（同 check_can_resume 的两次调用）：
      1. `check_can_resume` 里，排在 interrupt-and-resume 发 INT **之前**；
      2. `prepend_skill_guard` 里，作为结构性兜底——闸留在动作本身上，
         才没有一条绕过去的后门（cmd_run 根本不走 check_can_resume）。
    纯拒绝、无副作用，跑两遍不花钱。

    **判据用子串，不用整行**，这是对「本仓刚把轮次边界从子串改成整行」那条教训
    的**刻意例外**：那次要从混杂文本里解析**自己的标记**，误判会让判据说谎；
    这次要认的是**调用方复述了本工具的兜底句**，误判的后果是拒绝一个确实在谈
    skill 禁令的 brief——而那正是我们要拒的。失败方向是良性的，而且前移之后
    这个拒绝零副作用（见 check_can_resume）。
    **不要把它读成「任意措辞都挡得住」**——挡不住，实测见 SKILL_GUARD_STEM 那段。
    """
    if SKILL_GUARD_STEM in brief_text:
        reject(f"brief 里已经有兜底句（含「{SKILL_GUARD_STEM}」）。这句话归工具所有：\n"
               f"要放行哪些 skill 就用 --skill 逐条给（SKILL.md 的绝对路径，可重复），"
               f"一个都不给就用 --no-skill。")


def prepend_skill_guard(brief_text, skill_paths):
    """兜底句前置。CODEX_HOME 隔离是结构性防线，这句是内容层的第二道。

    两个参数都**没有默认值**：白名单每轮重给，缺省成空就等于替调用方做了决定。
    """
    check_brief_has_no_guard(brief_text)
    return f"{build_skill_guard(skill_paths)}\n\n{brief_text}"


def build_run_argv(dir_abs, effort, report_path, brief):
    # --cd 必须绝对路径：相对路径启动即崩（log 无 banner + os error 2）。
    # -o 也必须绝对路径：相对路径按「发命令那个 shell 的 cwd」解析、不按 --cd，
    # 实测在 --cd 的 worktree 里怎么找都没有，一度误判成「没正常收尾」。
    # 两条都由调用方传绝对路径进来（cmd_run 里 resolve），这里不做兜底猜测。
    # --color never：实测 --color auto（默认）在输出被重定向时**并不**关颜色，
    # 106 份日志无一例外含 ANSI。主线能从源头关掉，resume 关不掉（见下）。
    return (["codex", "exec", "--cd", dir_abs, "-m", MODEL,
             "-c", f'model_reasoning_effort="{effort}"',
             "--sandbox", "danger-full-access", "--color", "never"] + _COMMON +
            ["-o", report_path, brief])


def build_resume_argv(dir_abs, session_id, effort, report_path, brief):
    # 三处和主线不同，都是实测撞出来的：
    #   1. --cd 必须放在 resume 之前，放后面 clap 直接拒收
    #   2. resume 不认 --sandbox（error: unexpected argument，退出码 2），走 -c sandbox_mode
    #   3. resume 也不认 --color（2026-09-19 端到端冒烟：`error: unexpected argument
    #      '--color' found`，整轮当场死掉）。而 codex 没有对应的 config 键
    #      （--strict-config 探测回 `unknown configuration field \`color\``），
    #      所以 resume 这条路**关不掉颜色**——它的日志会带 ANSI，解析侧的
    #      strip_ansi 在这条路上是承重的，不是防御。
    # resume 收的参数集比主线小一圈，加参数前先 `codex exec resume --help` 对一遍。
    # 另：resume 总用 --cd／当前目录覆盖 workdir，不还原会话原目录，所以 --cd 必带。
    return (["codex", "exec", "--cd", dir_abs, "resume", session_id, "-m", MODEL,
             "-c", f'model_reasoning_effort="{effort}"',
             "-c", 'sandbox_mode="danger-full-access"'] + _COMMON +
            ["-o", report_path, brief])


def codex_env(home):
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    # 会话索引留在主目录，resume 才找得到（隔离的是 skill/plugin，不是会话历史）
    env["CODEX_SQLITE_HOME"] = str(pathlib.Path.home() / ".codex")
    return env


def meta_path(home, task):
    return home / "tasks" / f"{task}.json"


def write_meta(home, task, meta):
    """落元数据。**原子替换，不原地截断重写。**

    `Path.write_text` 先把目标截成 0 再往里填，而 `status` 随时可能在另一个
    进程里读同一份 json（`all_metas`／`find_meta` 都是裸 `json.loads`）。
    2026-09-20 实测截断重写的版本：1 秒里写 5289 次、并发读 12284 次，
    其中 **8277 次读到半截 json**——撞上的调用方拿到一个裸 `JSONDecodeError`
    traceback，不是干净的护栏拒绝。写进同目录的临时文件再 `os.replace`
    （同一文件系统上是原子的），读者就只可能看到「旧的那份」或「新的那份」。

    临时名固定、不带 pid：同一个任务不可能有两个并发写者——`cmd_run` 和
    `cmd_resume` 都先拒绝「还在跑」的同名任务。**那道闸查完到这里写完之间有个
    窗口**，而且自从等待插在闸之后（见 run_codex），这个窗口从几毫秒拉长到最多
    一个 `ROUND_END_TIMEOUT`。固定名在这种情况下依然是对的：两个写者会争同一个
    临时名，但 `os.replace` 是原子的，读者只可能看到完整的旧份或完整的新份；
    而固定名的好处是崩在中间留下的那一个残片会被下一次写盖掉，不会越积越多。
    后缀是 `.json.tmp` 而不是 `.tmp.json`：`all_metas` 扫的是 `*.json`，
    残片要是被扫进去，它自己就成了一份「缺字段的坏元数据」，
    而那条的爆炸半径是整个 status 列表（见 REQUIRED_META_KEYS 上方）。
    """
    q = meta_path(home, task)
    tmp = q.with_name(f"{q.name}.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    os.replace(tmp, q)


# **这两个必须留在 `REQUIRED_META_KEYS` 那一行之上，别往下挪。** 它是
# `tuple(new_meta(...).keys())`，在 **import 时**就跑一遍 `new_meta`，而
# `new_meta` 自取写者身份要调 `_writer_identity`——定义得比它晚，整个模块
# 当场 `NameError` 起不来（2026-09-20 实测过）。
# **PID 复用的余量是 445,255 倍，所以这里零行代码去防它。** 启动时刻的分辨率是
# 10ms（CLK_TCK=100），要让两副身份撞车就得在同一个 10ms 刻度内把同一个 PID 发两次，
# 而 PID 是顺序分配、绕完 pid_max 才回头：
#     pid_max 4,194,304 → 10ms 内绕完一圈需 4.19 亿次 fork/秒
#     本机实测 fork 速率 942 次/秒（绕一圈 1.2 小时）→ 余量 445,255 倍
# **别用「writer 陪跑整轮所以够长」来论证它——那个论证是错的**：write_meta 在
# Popen **之前**，Popen 一炸 writer 几毫秒就死。真正兜底的就是上面那个余量。
# 这个余量只覆盖**单次开机内**：starttime 是「自本次开机的滴答数」，重启会归零，
# 所以跨重启的陈旧元数据理论上能撞上一个无关的活进程。方向是安全的（拒绝而非
# 误放行——那个任务的 run/resume 会一直被拒），删掉元数据即可恢复。
def _read_stat_fields(pid):
    """`/proc/<pid>/stat` 里的 `(运行状态, 启动时刻)`，都是 `str`；进程不在了返回 `None`。

    **整文件读 bytes，从最后一个 `)` 之后切。禁止按行读、禁止 split() 全文。**
    `comm` 是进程自己用 `prctl(PR_SET_NAME)` 设的**任意 15 字节**：空格、括号、
    制表符、**裸换行**全都进得去（2026-09-20 实测 comm=`we ird)\\nx`，那时这个文件
    **按行读得到 2 行**，而第一行里最后一个 `)` 落在 comm 内部，切出来的字段列表
    长度是 0，下标 19 当场 IndexError）。它瞎得很安静——只在别人给进程改过名时才发作。

    **state 和 starttime 必须同一次读取出来**：分成两个函数会读两次 `/proc`，
    进程正好在两次之间变僵尸时，starttime 对得上、state 却是存活期那次的，
    「僵尸算停了」那一行当场失效。

    `tail[0]` 是 stat 的第 3 字段（state），`tail[19]` 是第 22 字段（starttime）：
    前两个字段 pid 和 comm 已经被切掉了，所以下标是 `22 - 2 - 1`。

    **这一对答的是两个不同的问题，别混成一个。** `starttime` 和 pid 合起来是
    **身份**（「是不是同一个进程」），它在进程的一生里恒定不变——僵尸期也一样。
    `state` 是**另一个维度**（「它还在干活吗」），R 和 S 都是「还在」，而且会
    随时来回变。2026-09-20 实跑踩过：拿整个元组去比对「改名前后解析是否一致」，
    陪练刚好从 R 翻到 S，测试就随机红一次——比对身份只许比身份那一半。

    `pid` 只是拼进路径，传 `str` 还是 `int` 都一样——这里不设闸，因为设了也
    换不来任何保证（返回值恒为 `str`，正确性不依赖入参类型）。
    """
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        # 进程随时可能退出（ENOENT），/proc 也可能读到一半没了——一律当「不在了」
        return None
    tail = raw[raw.rindex(b")") + 1:].split()
    return tail[0].decode(), tail[19].decode()


def _writer_identity():
    """**本进程**的身份 `(pid, 启动时刻)`，由 `run_codex` 盖进元数据。

    **成对返回，拿不到半截**：半截身份等于退回裸 PID，而裸 PID 挡不住复用。

    **类型问题用构造消掉，不用校验。** 落盘的那个值和比对时拿来对照的那个值
    都出自 `_read_stat_fields`，两边恒为 `str`，类型不匹配从构造上无从发生。
    （要是让它们各自产生，实测「存 int 比 str」会让 `"20096556" != 20096556`
    恒成立，一个还在排干的任务被**静默判死**——而 `_load_meta` 今天的设计注释
    明写「只校验键在不在」，为这两个字段开一道非对称的类型校验会把那条原则破掉。）

    只有 writer 记身份，codex 那侧继续用 `find_codex_pid` 现场反查。分界线是
    **有没有现场特征**：codex 有（comm=codex、argv 里有报告路径），而「还有没有人
    往日志里写字」没有任何现场特征。现场事实不依赖「有人记得写下来」，而 writer
    正是这条链上唯一会死的那个进程——所以只有它必须自报。

    读不到自己就直接炸：`/proc` 读不到自己意味着本工具的全部存活判定
    （`find_codex_pid` 也在内）都不成立，没有第二条路可走。
    """
    pid = str(os.getpid())
    fields = _read_stat_fields(pid)
    if fields is None:
        raise RuntimeError(f"/proc/{pid}/stat 读不到自己——本工具的全部存活判定都架在 /proc 上")
    return pid, fields[1]


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def new_meta(task, account, workdir, effort, skills):
    """元数据的**唯一**构造器。字段清单只在这里写一次。

    刻意没有 **codex 的** pid：它的存活必须每次现场反查（见 find_codex_pid），
    存下来的 PID 会过期、还会被系统复用，留着它只会诱导别人犯这个设计本来要防的错。

    **writer 的身份是另一回事，所以它有自己的名字。** 写日志的是包装器自己，
    而「它还在不在写」没有任何现场特征可查。身份是 **PID + 启动时刻**成对存
    （只存 PID 就退回上面那条防的老毛病）。

    **身份在这里自取，不收参数。** 本函数产出的是「一份**此刻开始**的任务的
    完整记录」，而「谁在写这一轮的日志」就是这份记录的一部分。
    收参数的那一版实测是**不可观测**的：`run_codex` 在 write_meta 之前会盖一次，
    传垃圾进来落盘的仍是真身份——于是「两个参数传反了」（两个都是 str，
    starttime 长得就像个 pid）永远没人发现。不可观测的参数就是给误用留的口子。

    `run_codex` 仍然会盖一次，**那不是第二个家**：两边调的是同一个
    `_writer_identity()`，值不可能不一致。它必须盖，是因为 `_resume_round`
    复用的是 `_load_meta` 从磁盘读回来的**旧记录**，根本不经过本函数——
    不盖章，resume 那两条路会落盘**上一轮**包装器的身份。

    `skills` 记的是**最后一次调用**的白名单，与 effort／started_at 同一条原则
    （见 _resume_round）。它是白名单唯一的结构化副本——刻意不进 status 的列：
    skill 路径是任意长度的绝对路径，进数据行会把定宽格式撑坏，要审计就读这里。
    """
    _require_skill_paths(skills)
    writer_pid, writer_start = _writer_identity()
    return {"task": task, "account": account, "dir": workdir, "effort": effort,
            "skills": skills, "session_id": None, "started_at": _now_iso(),
            "writer_pid": writer_pid, "writer_start": writer_start}


# 校验面由构造器派生，**不另写一份清单**。两份清单必然漂移，而漂移的后果是
# 静默的：少一个字段，run 照常报成败，但那个任务从此 status/resume/stop 全
# 够不着，工具还会建议「删掉它重新 run」——会话就此丢掉。
#
# **爆炸半径是「列表」，不是「那一个任务」**（2026-09-20 逐条实测）：
#   all_metas()        一个坏 json 就整条拒绝 → 不带任务名的 `status` 一个任务
#                      都列不出来，连好的那些一起陪葬
#   find_meta("好的")   **不受牵连**——它只 stat/读那一个任务的 json
#   find_meta("坏的")   拒绝，而这是对的：你点名要的就是那一份
# 所以 `status <任务名>`／`resume`／`stop` 只对坏掉的那个任务失灵。
#
# **往 REQUIRED_META_KEYS 加字段之前，先确认没有在跑的任务**：_load_meta 对缺
# 字段的老元数据只会建议「删掉它重新 run」，而那等于丢会话。2026-09-19 加
# `skills` 那次实测三个隔离目录的 tasks/ 全空（各 0 个 json），所以影响为零——
# **那是当时的事实，不是永久豁免**，下次加字段要重新确认一遍。
# 2026-09-20 加 writer_pid／writer_start 那次同样执行了这条仪式：三个隔离目录
# tasks/ 共 0 个 json（~/.codex-subagent 空，-acct2／-acct3 连 tasks/ 都没有），
# 当前用户的 codex 进程无一属于本工具 → 本次爆炸半径为零。
# 同上：**那是当时的事实，不是永久豁免**。
# 读回来就校验，之后所有地方放心裸下标；`.get(键, 默认值)` 是默认缺省值，
# 正是本工具要消灭的东西。
REQUIRED_META_KEYS = tuple(new_meta("", "", "", "", ()).keys())


def _load_meta(path):
    meta = json.loads(path.read_text())
    missing = [k for k in REQUIRED_META_KEYS if k not in meta]
    if missing:
        reject(f"{path} 缺字段 {missing}，元数据坏了——删掉它重新 run")
    return meta


def find_meta(task):
    """跨所有隔离目录按任务名找，返回 (home, meta)；查不到返回 (None, None)。

    账号是查出来的，不是让调用方再报一遍的。
    home 走返回值，不塞进 meta 里当 `_home` 魔法键：魔法键意味着每个写回元数据
    的地方都得记得把它剥掉，而「必须记得」正是这个工具存在的理由本身。

    查到多份就拒绝，不"取第一个"：那会让 status/resume/stop 静默作用到扫描顺序
    更靠前的那个会话上。而任务名撞车这条路很好走——撞额度上限就该换账号重跑。
    cmd_run 已经不让这个状态建起来，这里是第二道。
    """
    found = []
    for account in account_choices():
        home = isolation_home(account)
        p = meta_path(home, task)
        if p.exists():
            found.append((home, _load_meta(p)))
    if len(found) > 1:
        reject(f"任务名 {task} 在多个隔离目录里都有："
               + "、".join(str(h) for h, _ in found)
               + "\n无法确定该操作哪一个，删掉不要的那份元数据再来。")
    return found[0] if found else (None, None)


def all_metas():
    out = []
    for account in account_choices():
        home = isolation_home(account)
        tasks_dir = home / "tasks"
        if not tasks_dir.is_dir():
            continue
        for p in sorted(tasks_dir.glob("*.json")):
            out.append((home, _load_meta(p)))
    return out


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        # EPERM 是「有这个进程，但不归你管」，不是「已退出」。把它当死，就会
        # 误判「已结束」而去 resume 一个还在跑的会话，撞上它自己的写锁。
        return True
    return True


def find_codex_pid(report_path):
    """存活判定只有一个可靠判据：真实 PID。日志判不了，$! 给不出。

    回合用尽的 codex 留下的 log 和还在跑的长得一模一样（末尾都是正常输出、
    没有收尾标记），所以 tail 日志只能看它在干什么，判不了存活。
    $! 拿到的是包装链最外层（2026-09-17 实测：$! 是 254151，codex 是 254153），
    据此判"已退出"再 resume，会撞上它自己的写锁。

    反查直接扫 /proc，**不用 pgrep**。三条理由，每条都是承重的：

    1. `pgrep -f <模式>` 的模式是**正则**，而报告路径里有 `.`（任务名允许点，
       后缀又是 `.md`）。2026-09-19 实测：任务 `a` 的 `…/reports/a.md` 拿去
       pgrep，命中了任务 `aXmd` 的 `…/reports/aXmd.md`——`a`+任意字符+`md`。
       后果是 `stop a` 把 SIGINT 发给 aXmd 的 codex。转义救不了根：这里要的
       根本不是匹配，是**相等**。报告路径在 codex 的 argv 里正好是独立一项
       （`-o <路径>`），所以按 argv 元素精确比对，正则语义一点都不引入。
    2. comm 必须是 codex：pgrep -f 会命中发命令的 shell 自己（2026-09-19 实测，
       comm=bash），少了这道过滤会把 shell 当成 codex。
    3. 必须限当前用户：本机有别的用户在跑 codex，不限的话 stop 会打到别人身上。

    顺带省掉每次 1+N 次子进程（一个 pgrep 加每个候选一个 ps）。
    """
    me = os.getuid()
    # str() 一道：报告路径在本模块里一律以 pathlib.Path 传递，这里是唯一的落地点。
    # 全模块只在这里从 Path 落到 str：别处一律传 Path（_wait_previous_round_ends
    # 连报告路径都不收，自己从 (home, task) 派生）。同一个东西两种传法，
    # 正是「好 API 难被误用」要消掉的那种缝。
    needle = str(report_path).encode()
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # 进程随时可能退出，每一步都可能 ENOENT——一律跳过，不让它打断扫描
            if entry.stat().st_uid != me:
                continue
            if (entry / "comm").read_bytes().strip() != b"codex":
                continue
            if needle not in (entry / "cmdline").read_bytes().split(b"\0"):
                continue
        except OSError:
            continue
        pid = int(entry.name)
        if pid_alive(pid):
            return pid
    return None


def _previous_writer_alive(home, task):
    """磁盘上那份元数据记的那个进程，现在还在吗。

    **名字答的就是它知道的事实**：不是「还有人写日志吗」——那是调用方拿它去回答的
    问题。收 `(home, task)` 不收 dict：两个调用点（`_wait_previous_round_ends` 的
    轮询、`run_codex` 超时后的那句诊断）都在 `run_codex` 里，都由它自己那一对
    `(home, task)` 派生，**没有第二个来源**；而收 dict 就可以传错任务，
    传错了它会一声不吭地答「停了」。

    四条判停，缺一不可：

        文件不在        没有上一轮
        记的就是本进程  那一轮的 writer 就是我自己，见下
        启动时刻对不上  PID 被复用了（裸 os.kill 在这里会说「活着」）
        state == "Z"    僵尸

    **第四条是承重梁，不是锦上添花。** 实测：僵尸期的 `starttime` 与存活期
    **完全相同**，`os.kill(pid, 0)` 也照样「成功」——没有这一行，等的就变成
    「等它被父进程回收」，而回收时机归 harness 的 bash/node 管，是第三方。
    spec 的版本小史里 v4 就是栽在这条前提上。
    """
    q = meta_path(home, task)
    if not q.exists():
        return False
    previous = _load_meta(q)
    # **上一轮要是本进程写的，那它已经写完了。** 写日志的包装器就是跑 run_codex
    # 的这个进程（tee 循环在它自己身上），所以它一落盘，磁盘上那副身份就是它自己。
    # run_codex 单线程顺序执行：能走到这里，本进程上一轮的 tee 循环早已返回。
    # 不加这一行，同一个进程连跑两轮就会**等自己**——白等满一个 ROUND_END_TIMEOUT，
    # 再拿到一句**说谎的诊断**（「包装器还在读 codex 的输出」，而那个包装器就是
    # 问话的人自己）。实测超时压到 0.5s 时第二轮就是这么被自己挡住的。
    # 这一行不是注释能代替的：失败模式是 60 秒静默挂起（等待排在分隔符之前，
    # 屏幕和日志都是死的）。**但要说准它保护的是谁**：CLI 路径上一个进程一生
    # 只调一次 run_codex（三个 cmd_* 各走其一），磁盘上那副身份不可能是本进程，
    # 所以这一支在生产里**不可达**。它保护的是同进程重复调用——单测，以及哪天
    # 有人把本模块当库用。留着的代价是一行，换来谓词对这类用法也是全函数。
    if (previous["writer_pid"], previous["writer_start"]) == _writer_identity():
        return False
    fields = _read_stat_fields(previous["writer_pid"])
    if fields is None:
        return False
    state, start = fields
    return start == previous["writer_start"] and state != "Z"


# codex 全集是 minimal/low/medium/high/xhigh/max/ultra，这五档是**刻意裁剪**。
# argparse 的 choices 是唯一守门员——实测 codex 对 `-c model_reasoning_effort=bogus`
# 静默接受、banner 照打 `reasoning effort: bogus_effort_value`，档位写错没人告诉你。
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# 五个状态回答的是同一个问题：**接下来该干什么**。
#   success 0 不用干什么 / running 4 等 / interrupted 130 **接着 resume** /
#   suspect 3 去看一眼 / failed 1 查原因并重跑
# interrupted 的处置和其余四个都不同，所以它是一个状态，不是 failed 下的一条理由。
#
# 它推翻的是一个写明了理由的旧决定：`assertEqual(v.state, "failed")  # 产物确实
# 没出来，状态不变`。正面回应——产物没出来是事实，但状态回答的不是「产物出来没
# 有」，而是「接下来该干什么」，五个状态都是按这个轴分的。
# 值得动退出码，是因为 harness 的完成通知**只搬退出码、不搬 stdout**：reason 再
# 准确也到不了做决定的那一方，而在唯一到得了的那个通道上，interrupted 和 failed
# 此前是同一个数字、处置却相反。实测代价：被打断的探针已烧掉 28,107 tokens，
# 退出码 1 会让照契约做决定的 agent 从头重跑，那 28k 连同保住的上下文一起扔掉。
#
# 顺序即严重度（越靠后越该拦住调用方），退出码和严重度都从这**一份**派生——
# 两份清单必然漂移。**interrupted 必须排在 running 之后**：在跑的任务会自己好，
# 被打断的永远不会自己好——它在等人动手。status 列一批任务时若被 running 盖住，
# 调用方会去「等」一个永远不会自己好的东西。
# 严重度不能直接拿退出码比：suspect 的码(3)比 failed(1)大，按码取 max 会让一个
# 真失败被一个 suspect 盖过去；码值与顺序无关。
# 退出码取值一律 EXIT[state]，不写 .get(state, 默认值)：有默认值的话 running 会
# 悄悄落成 0，`codex-sub-agent status t && deploy` 就会在任务还在跑的时候部署。
# 2 永久留给参数错误与护栏拒绝（见 USAGE_ERROR），不进这张表。
#
# interrupted 用 **130** 而不是自编一个数：128+SIGINT(2) 是 POSIX/Bash 的既成
# 约定（同族 SIGKILL→137、SIGTERM→143），脚本作者和 agent 不读本文档也认得。
# 反面要知道：>128 在约定里指「**本进程**死于信号」，而这里死的是 codex、
# 包装器是正常退出的。取它是因为读者的第一反应正确（「被打断了，接着来」），
# 比语义上的精确更重要——退出码是唯一能到达 harness 完成通知的通道。
# 3/4 仍是自编：suspect、running 这两个概念约定里根本没有。
# 原则是：**能跟约定的跟，约定没涵盖的才自己编。**
#
# 退出码值得这么较真，是因为它**真的会被人看见**：实测 harness 给后台任务的
# 完成通知里直接带着退出码（exit 3 的那条通知写的就是
# `failed with exit code 3`）。所以「run 收尾自己跑一遍判据、退出码＝判据结论」
# 这件事等于把结论直接送到了调用方眼前——happy path 下根本不用再敲 status，
# 而 `exit 1 ≠ 失败` 这条最反直觉的知识也就被彻底消化掉了。
_STATES = (("success", 0), ("running", 4), ("interrupted", 130), ("suspect", 3), ("failed", 1))
EXIT = dict(_STATES)
_SEVERITY = [name for name, _ in _STATES]


def _worse(a, b):
    return a if _SEVERITY.index(a) >= _SEVERITY.index(b) else b


# session id 在 banner 里，前几百字节就出现。攒到这个上限还没有就不再攒，
# 免得几 MB 的输出全堆在内存里。
_HEAD_LIMIT = 8192


def _report_path(home, task):
    # .md 不是 .json：`-o` 写的是 agent 的最后一条消息，实测 156 份里只有 4 份
    # 能解析成 JSON，其余都是 markdown 散文。后缀名要说真话。
    return home / "reports" / f"{task}.md"


def _log_path(home, task):
    return home / "logs" / f"{task}.log"


# 开跑前最多等上一轮安静下来多久。**这是策略值，不是实测上界。**
# 排干多久由 codex 起的后代决定，**没有上界**——它起个后台服务就永不结束。
# 所以这个数字回答的是「本工具愿意等多久」：够大到不会误杀正常收尾
# （2026-09-19 实测 INT → codex 消失是 1.854 秒和 0.964 秒，writer 再多几秒），
# 够小到卡住时调用方不会被无限期挂着。**不要拿实测值去给它钉倍数余量。**
#
# 名字去掉了 `INTERRUPT_` 前缀：等待已经不是 interrupt-and-resume 独有的，
# run / resume 走的是同一条（见 run_codex）。
#
# 必须是**模块级常量**而不是函数默认参数：测试要把它压到 0.3 秒；
# 也正合仓库规范「不要默认缺省值」——调用方每次都显式传，不可能漏。
ROUND_END_TIMEOUT = 60
ROUND_END_POLL_INTERVAL = 0.2


def _wait_previous_round_ends(home, task, timeout, poll_interval):
    """等到**上一轮的 writer 停了**、而且**codex 也停了**。返回是否等到。

    两个条件问的是两件事，缺一不可：

        还有人写日志吗   _previous_writer_alive   包装器，身份记在元数据里
        会话还被占着吗   find_codex_pid           现场反查

    只看 codex 会在它**变僵尸那一刻**（实测 0.027s）就放行——僵尸的 cmdline 为空，
    argv 精确比对天然拒绝它——而那时日志还要再长好几秒。只看包装器会撞上
    **孤儿 codex**：SIGKILL 掉包装器之后 codex 存活并跑完（实测），续跑撞上它的写锁。

    **不用 pidfd，直接轮询。** codex 那半没有记下来的身份（刻意的：它有现场特征，
    而现场事实不依赖「有人记得写下来」），只能轮询扫 /proc——**整个函数的唤醒粒度
    本来就被 poll_interval 钉死了**，pidfd 在 writer 那半最多买到一个轮询间隔，
    代价却是内核版本前提 + ENOSYS 拒绝路径 + 三条 TOCTOU 契约。一分钱没买到。

    **只等，不发任何信号。** 超时的正确处置是让调用方稍后再来，不是加大火力：
    升级到 SIGTERM 会让会话永久锁死，而那一步不可逆。

    报告路径**自己从 (home, task) 派生**，不收参数：收参数就能传错（把日志路径传
    进来的话 find_codex_pid 永远找不到，等待整个变成空操作），而这里只有一个调用点。

    **没有「元数据不在就直接放行」这条捷径。** 那条早返回既多余（`_previous_writer_alive`
    自己第一件事就是查文件在不在），又**开洞**：它连 codex 那一半都跳过了——
    元数据被手删、而上一轮的 codex 还占着会话时当场放行，新一轮撞上它的写锁。
    两个条件一个都不能少。
    """
    report = _report_path(home, task)
    deadline = time.monotonic() + timeout
    said = False
    while True:
        if not _previous_writer_alive(home, task) and find_codex_pid(report) is None:
            return True
        if not said:
            # **只在真要等的时候说，而且只进屏幕不进日志。** 等待排在分隔符之前，
            # 日志里此刻还没有本轮的边界，写进去就落在上一轮里，把上一轮的判据弄脏。
            # 不说的话 run 会多出一个全新的**静默挂起**。它当场读得到，靠的是
            # main() 里那行 sys.stdout.reconfigure(line_buffering=True)。
            print(f"[codex-sub-agent] {task} 上一轮还在收尾，最多等 {timeout} 秒…")
            said = True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def run_codex(kind, home, task, meta, make_argv):
    """唯一的 spawn 入口。**返回一个 `Round`**：报告路径 + 本轮日志文本
    （codex 退出那一刻的快照）。

    开跑前必须做的三件事全在这里，调用方不需要记住顺序：
    ① 元数据落盘 ② 删掉上一轮的报告 ③ 日志追加一行本轮分隔符。

    **报告路径由本函数拥有**，回传给 `make_argv` 去拼命令；`env` 也由 `home`
    派生。原签名收现成的 `argv` 和 `env`，自己却又重新推导一遍报告路径去删，
    于是留下三条没人保证的「必须记得对齐」：argv 里 `-o` 指的那个文件、被删掉
    的那个文件、元数据文件名指的那个任务，得是同一个。三条一起错时它一声不吭
    ——clear_report 删了别的文件（防陈旧报告这条 P0 静默失效）、日志分隔符说谎、
    元数据内容和文件名对不上。现在这三条在结构上就违反不了了。

    这三件事原先还散在调用方，实测漏掉「先 write_meta」会在 codex **已经跑起来
    之后**才炸 FileNotFoundError，子进程当场变孤儿。

    （stdin／timeout／start_new_session 各自的理由写在它们那一行旁边。）
    """
    # 校验排在 write_meta／clear_report／spawn **全部之前**：任何一件先发生，
    # 失败就会留下半个状态（元数据落了盘、上一轮报告被删掉，而 codex 没起来）。
    _require_enum(kind, _KINDS, "kind")
    report = _report_path(home, task)
    argv = make_argv(str(report))
    env = codex_env(home)

    # 开跑前的三件事，全部在 spawn **之前**做完：任何一件炸了，codex 都还没起来，
    # 不会留下一个没人管的孤儿进程。
    # 开跑前的第一件事：**确认上一轮真的结束了**。
    # 等谁**从磁盘读**（见 _previous_writer_alive），绝不用传进来的 meta 参数：
    # cmd_run 传的是刚造好的 new_meta()，那一份记的是本进程，按参数读就等错了人
    # ——等的成了「参数里写的那个」而不是上一轮真正的写者。
    # **传进来的 meta 只用于写新的那份。**
    #
    # 这一等排在 write_meta / clear_report / 分隔符 **全部之前**，所以超时**不留
    # 半个状态**：上一轮的报告还在、日志没被加分隔符、元数据还是上一轮那份、
    # codex 一个都没起。这是本次改动最值钱的性质，别把它挪到后面去。
    #
    # 折进 run_codex 而不是逐个命令补闸：它是**唯一的 spawn 入口**，run / resume /
    # interrupt-and-resume 三条路都经过它，于是「哪个入口漏了」结构上不存在。
    # 从前只有 interrupt-and-resume 会等，而 cmd_resume 同样只看 codex——
    # 上一轮被打断、codex 已没、writer 还在排干时它当场放行，老 writer 的输出
    # 会落在新分隔符之后，judge 把上一轮的尾巴算成这一轮。同一类 bug 换个入口。
    if not _wait_previous_round_ends(home, task, ROUND_END_TIMEOUT, ROUND_END_POLL_INTERVAL):
        # 超时之后**再问一次两个谓词**：两种停不下来的现场不同，而这行 stderr
        # 是调用方唯一的线索。不让等待函数回一个成因枚举——那会多出一个实体，
        # 而「谁还没停」本来就是这两个谓词各问一次的事。
        busy = []
        if _previous_writer_alive(home, task):
            busy.append("包装器还在读 codex 的输出（codex 起的后台进程继承了同一个 "
                        "stdout，它不退管道就不 EOF）")
        if find_codex_pid(report) is not None:
            busy.append("codex 本身还在跑")
        if not busy:
            busy.append("刚刚才安静下来——超时和它停下撞在了一起")
        下一步 = ("稍后重跑这条命令即可——它不会再发第二发 INT（现有的闸会挡），"
                  "**也不要再 stop**。绝不升级信号：SIGTERM 会让会话永久锁死，不可逆。"
                  if kind == "interrupt-and-resume" else
                  f"等上一轮收尾完再来；codex 也还活着的话先 `codex-sub-agent stop {task}`"
                  f"（run 还可以换个任务名）。")
        reject(f"任务 {task} 的上一轮还没安静下来（等了 {ROUND_END_TIMEOUT} 秒）："
               + "；".join(busy) + "。\n" + 下一步)

    # writer 身份在这里盖，**而且只在这里**：run_codex 是唯一的 spawn 入口，
    # run / resume / interrupt-and-resume 三条路都经过它，调用方不需要记住任何事。
    # 贴在 write_meta 正上方，是因为「落盘那一刻，writer 身份 = 落盘的这个进程」
    # 这条不变式只有贴在这里才看得见。
    # resume 路上的 meta 是 `_load_meta` 从磁盘读回来的，里面是**上一轮**包装器的
    # 身份，那个进程早就死了——不盖章，下一条命令问的就是上上轮那个进程的事。
    meta["writer_pid"], meta["writer_start"] = _writer_identity()
    write_meta(home, task, meta)
    clear_report(report)

    # 日志追加不覆盖，先写一行本轮分隔符——判据只扫它之后的内容。
    # 分隔符由本函数自己写，调用方不可能忘；忘了判据就会把上一轮的错误算到这一轮头上。
    with open(_log_path(home, task), "ab") as log:
        # 分隔符也必须落在行首，理由同 interrupt_codex 的前置换行：上一轮的
        # 尾巴可能被 INT 截在半行，分隔符接上去就不在行首，_ROUND_LINE 认不出，
        # read_last_round 于是把两轮连成一轮，上一轮的错误算到这一轮头上。
        # 文件刚以 "ab" 打开，tell() 就是文件长度——**非空才补**，
        # 新日志的开头不该有空行（那条 startswith(ROUND_MARK) 的断言也才保得住）。
        #
        # 这个 tell() 是个**快照**，它正确靠的是「日志为空时不存在第二个写者」。
        # 这条事实散在别处四个地方，所以在这里列出来——不列的话，下一个人得靠
        # 读另外四个函数才能确认这里没问题，而「读者根据别处的事实推断」正是
        # 本轮修掉的那三层 bug 的共同形状：
        #   1. cmd_run 撞见同名任务还在跑就拒绝，所以不会有两个 run 共写一份日志
        #   2. interrupt_codex 只在 os.kill **成功之后**才写，而能被 kill 的 codex
        #      必然已经有过一轮分隔符 → 日志非空
        #   3. cmd_stop / cmd_interrupt_and_resume 都先 find_meta，查不到就拒绝；
        #      查得到就说明至少跑过一轮 → 日志非空
        #   4. interrupt_codex 的写入恒以 \n 结尾，所以它留下的末尾永远在行首，
        #      不会让后来的 tell() 看到一个「非空但不在行首」的状态
        # 仓内不可达。哪天加了新的写者（比如并发的同名任务），先回来看这四条。
        if log.tell() > 0:
            log.write(b"\n")
        log.write((round_separator(kind, task, _now_iso()) + "\n").encode())
        log.flush()
        # 本轮的起点：分隔符之后的第一个字节。
        # O_APPEND 下每次 write 都是「原子地跳到末尾再写」，所以即使别的进程
        # 正往同一个日志追加（留痕、另一轮的分隔符），这个位置依然精确指向
        # **我们自己刚写的那行之后**——拥有者的边界由此成为事实而非推测。
        start_offset = log.tell()

        # start_new_session=True 不是为了 detach，是为了挡**组信号**。2026-09-19 实测：
        # codex 与包装器同进程组时，一发 `kill -TERM -<组>`（harness 停掉后台 Bash 任务
        # 就是这么干的）会直接把 codex TERM 死，而 SIGTERM 之后 thread 永久锁死、
        # 再也 resume 不了、上下文全丢。隔到独立会话后 codex 收不到任何组信号，
        # 只会收到下面 handler 转发的 INT。
        #
        # stdin 固定接 /dev/null：否则 codex 等 stdin 永久挂死（日志只剩
        # "Reading additional input from stdin" + 进程 0% CPU）。
        # 不设 timeout：会误杀正当的长任务。
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)

        def forward_as_sigint(signum, frame):
            # 无论包装器被谁、用什么信号停，codex 收到的永远是 INT，上下文永远可 resume，
            # 而且日志里一定留下痕迹——之后跑判据的人（包括另一个进程里的 status）
            # 才知道这轮该 resume 而不是重跑。
            # 刻意不在这里退出：让 tee 循环自然跑完，判据照样出、完成通知照样带结论。
            # 来源写「外部信号转发」：在 run_in_background 下这条路根本不该
            # 走到——它出现在日志里，就是前台误跑被超时杀掉的诊断。
            interrupt_codex(proc.pid, _log_path(home, task), "外部信号转发")

        # 转发只在 codex 活着的这段时间里生效，出去时原样还回去——改全局信号处置
        # 而不还原，等于把本函数的副作用留给了整个进程的余生。
        previous = {sig: signal.signal(sig, forward_as_sigint)
                    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        try:
            _tee_until_exit(proc, log, home, task, meta)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    # 回传**报告路径 + 本轮日志文本**这一对，而不是偏移。
    # 偏移是可以被悄悄丢掉的：调用方忘了接，唯一还能拿到本轮文本的路就是
    # read_last_round——正好是这次要修的那个 bug。文本丢不掉，它就是 judge 的参数。
    # 成对回传是因为报告路径本来就归本函数所有（见上），调用方再算一遍同一个
    # 路径就又冒出一条「必须记得对齐」；而配错是静默算对的（理由见 Round）。
    # 副带好处：快照在 codex 退出那一刻取走，比「调用方稍后自己读」窗口更小。
    return Round(report, read_round(_log_path(home, task), start_offset))


def _tee_until_exit(proc, log, home, task, meta):
    head, session_id = b"", None
    # read1：有数据就返回，不等凑满。用 read 会阻塞到满 1024 字节或 EOF——
    # codex 的 banner 只有 ~170 字节，之后可能思考几十分钟，这期间屏幕、日志、
    # 元数据里的 session id 全是空的（实测父进程 4.06 秒才看到 t=0 就 flush 的
    # 172 字节）；包装进程此时被杀，这一轮就再也 resume 不回来。
    # 这个循环等的是 **stdout 管道 EOF**，不是「codex 进程没了」：codex 起的
    # 孙进程继承同一个 stdout，孙进程不退管道就不 EOF。**这是刻意保留的**——
    # 「日志什么时候安静下来」的答案就等于「本循环什么时候退出」，所以下一轮
    # 开跑前要等的是**本进程**，不是 codex。本进程的身份就落在元数据的
    # writer_pid／writer_start 里（见 run_codex 里盖章那一行），
    # _wait_previous_round_ends 等的也正是它。
    # 实测：codex 变僵尸是 t=0.027s，而那一刻 find_codex_pid 就判 None
    # （僵尸的 cmdline 为空，argv 比对拒绝它），日志还要再长好几秒。
    for chunk in iter(lambda: proc.stdout.read1(1024), b""):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        log.write(chunk)
        log.flush()
        if session_id is None and len(head) < _HEAD_LIMIT:
            head += chunk
            session_id = extract_session_id(head.decode("utf-8", "replace"))
            if session_id:
                meta["session_id"] = session_id
                write_meta(home, task, meta)
    proc.wait()


# status 数据行的列序是**机器切分的契约**：前四列无空白，所以
# `line.split(maxsplit=4)` 精确切出它们，第五段是「工作目录 + reason」。
# **7 个 Verdict 构造点里有 4 个的 reason 含空格**（「本轮被 INT 打断，上下文
# 保留——接着 resume 即可，不用重跑」、「报告在，但本轮日志有 N 条未分类的
# codex 错误」、「会话被写锁占住（……曾被 SIGTERM 杀过），只能新起一个任务」、
# 「pid=N 存活」），所以 reason 必须排在最后——这就是这次换列序的全部理由。
# dir 与 reason 因此不可分，可以接受：dir 早就在 tasks/<task>.json 里，
# 调用方真正要的是 state 和退出码。
#
# **刻意不换格式。** 初稿的制表符方案被实测否掉：按 tabstop=8 量四行真实输出
# 的各列屏幕起始列，[0,16,24,32,40,80] / [0,16,24,32,40,88] / [0,8,16,24,32,40]
# / [0,32,40,56,64,80]——四行没有一列对齐；而空格定宽是稳定的 [0,25,34,43]
# （那是**换列序之前**的四列格式量出来的；定宽的稳定性与列数无关，结论照样成立）。
#
# 退出码单独成列：它和 state 是同一份事实的两种编码，但**同源派生**（都来自
# EXIT[state]），不存在漂移风险。人读词，机器读码。
#
# 白名单（skills）刻意**不进列**：skill 路径是任意长度的绝对路径，进数据行会
# 把定宽撑坏。要审计就读 tasks/<task>.json，那本来就是结构化的。
_WHITESPACE = re.compile(r"\s")


def status_row(meta, verdict):
    """拼一行数据行，**拼之前先断言它切得开**——这条不是碰巧成立的。

    收的是 `meta` 和 `verdict` 两个对象，不是五个位置 `str`：五个同类型参数里
    **把 workdir 和 reason 传反会静默拼出旧列序**，而那正是这次改动要消灭的东西。
    调用方（cmd_status）手里正好就是这两个对象，顺序传不反。

    前四列断言「一个空白都没有」：`split(maxsplit=4)` 靠的就是它。
    状态和退出码由上面那条 `_require_enum` 保证（五态里没有一个含空白），
    **剩下的任务名和账号两列都没人保证，两条断言都是承重的**：
    这两列读的都是 `tasks/<task>.json`，而 `_load_meta` 只校验**键**在不在，
    值长什么样一概不管——2026-09-20 实测手写一份 `"task": "t 1"` 的元数据，
    `_load_meta` 一声不吭收下，挡住它的只有下面那条断言。
    `--skill` 的 `_TASK_NAME` 和 `account_choices()` 守的是**另一个入口**
    （命令行与目录扫描），手写的 json 从它们旁边绕过去，所以这里不是重复防御。

    后两列断言「没有控制字符」：dir 和 reason 里空格是合法的（它们同在第五段），
    换行和制表符不是——一个换行就让「一行一任务」不成立，任何切分方案都救不
    回来。`--dir` 在入口已经挡过一道（见 work_dir），reason 当前也恰好不含
    （穷举 Verdict 的 7 个构造点确认过），但那是**碰巧成立、无人守卫**。

    明细行（错误行与报告预览）不走这里：它们由 cmd_status 加缩进打印，
    首字符是空白，从而与数据行结构性可分；而它们的内容来自 splitlines()，
    结构上不可能含换行。
    """
    # 先过枚举：退出码那一列是 EXIT[state]，裸下标对写错的状态只给一个 KeyError，
    # 调用方看不出是什么坏了。和 kind／cause 同一条做法。
    _require_enum(verdict.state, tuple(EXIT), "state")
    task, account, workdir = meta["task"], meta["account"], meta["dir"]
    for label, field in (("任务名", task), ("账号", account)):
        if _WHITESPACE.search(field):
            reject(f"{label} {field!r} 含空白字符——status 的前四列就切不开了，"
                   f"调用方再也取不出 state。")
    for label, field in (("工作目录", workdir), ("reason", verdict.reason)):
        if _CONTROL_CHARS.search(field):
            reject(f"{label} {field!r} 含控制字符——「一行一任务」就不成立了。")
    return (f"{task:<24} {account:<8} {verdict.state:<8} {EXIT[verdict.state]:<4} "
            f"{workdir}  {verdict.reason}")


def _print_verdict(task, verdict):
    print(f"\n[codex-sub-agent] {task}: {verdict.state} —— {verdict.reason}")
    for line in verdict.detail:
        print(f"  {line}")


def cmd_run(args):
    workdir = pathlib.Path(args.dir).expanduser().resolve()
    # 入口那道（work_dir）守的是**命令行上那个原始串**，而落进元数据、随后进
    # status 数据行的是 resolve() 之后的真身——软链一跨就绕过去了。实测：
    # --dir 指向一个软链，真身叫 `a\tb\nc`，入口放行、resolve 出来含 \t\n、
    # 落盘，之后 status 整条被 status_row 拒掉，一个任务都列不出来。
    # 这里用 reject 而不是 ArgumentTypeError：此刻已经离开 argparse 了，
    # 而 Rejected 和参数错误本来就同一个退出码 2，不必多一个实体。
    hit = _CONTROL_CHARS.search(str(workdir))
    if hit:
        reject(f"--dir {args.dir} 解析出来的真身 {str(workdir)!r} 含控制字符 "
               f"{hit.group()!r}（软链？）。它要原样进 status 的数据行，会把「一行一任务」切坏。")
    if not workdir.is_dir():
        reject(f"--dir {args.dir} 不是目录")
    brief_file = pathlib.Path(args.brief).expanduser()
    if not brief_file.is_file():
        reject(f"--brief {args.brief} 不是文件（brief 只收文件路径，避开引号地狱）")

    old_home, old_meta = find_meta(args.task)
    if old_meta is not None and find_codex_pid(_report_path(old_home, args.task)) is not None:
        # 「还在跑」只查一次，**在挑账号之前**：它问的是「这个任务名此刻有没有
        # 活着的 codex」，和最后跑哪个账号无关。**这不替代 run_codex 内部每次
        # spawn 前的等待**——那一条问的是「上一轮的 writer 和 codex 排干了没有」。
        reject(f"任务名 {args.task} 还在跑，换个名字或先 `codex-sub-agent stop {args.task}`")

    if args.account == AUTO:
        candidates = accounts_by_availability()
        if not candidates:
            reject("没有任何账号有登录态，auto 模式无从分配：\n"
                   + "\n".join(f"  {a} 缺 {auth_source(a)}" for a in account_choices())
                   + "\n先跑 `codex-acct login <账号>`。")
        account = candidates[0]
    else:
        account = args.account
        # 跨账号同名的护栏**只在强制模式下需要**：同一个名字出现在两个隔离目录里
        # 时，find_meta 会数出两份并拒绝，而另一份就成了再也够不着的孤儿元数据。
        # auto 模式不走这条——它不问任务住在哪（见 accounts_by_availability），
        # 挑中别的账号时在下面**显式**把旧的那份搬过来。
        if old_meta is not None and old_home != isolation_home(account):
            reject(f"任务名 {args.task} 已经属于账号 {old_meta['account']}（{old_home}）。\n"
                   f"同名任务跨账号会让 status/resume/stop 指向哪个变得不确定，换个任务名。\n"
                   f"要换账号就用 --account {AUTO}，它会把元数据搬过来。")

    home = ensure_isolation(account)
    if old_meta is not None:
        if old_home == home:
            print(f"[codex-sub-agent] 提示：任务名 {args.task} 复用，"
                  f"上一轮的报告会被删掉、日志会被追加")
        else:
            # 只有 auto 走得到这里（强制模式上面那道护栏已经拒了）。
            # **措辞必须点明旧产物留在原处。** 上面那句「上一轮的报告会被删掉」
            # 在跨账号时是**假话**：`clear_report` 只动这一轮要写的那个报告
            # （新 home 的），旧 home 的 reports/ 和 logs/ 一个字节都没碰。
            # 搬的只有元数据——不搬的话同一个名字出现在两个隔离目录里，
            # find_meta 当场数出两份，status/stop 从此全退 2。
            print(f"[codex-sub-agent] 任务 {args.task} 原本在账号 {old_meta['account']}，"
                  f"本次改用 {account}：元数据搬过来，"
                  f"旧账号的报告和日志留在 {old_home} 原处不动。")
            meta_path(old_home, args.task).unlink(missing_ok=True)

    # 不再打印兜底句：它每轮派生、有白名单时是多行，而「这一轮给了哪些 skill」
    # 的权威副本在元数据的 skills 字段里（见 new_meta）。印第二份只会漂移。
    brief = prepend_skill_guard(brief_file.read_text(), args.skills)
    # 本轮的拥有者：judge 收的就是 run_codex 回传的那一对，**不用 read_last_round**
    # ——后者是外部观察者的上界，拥有者用它就是把事实换回推测。
    this_round = run_codex("run", home, args.task,
                           new_meta(args.task, account, str(workdir), args.effort, args.skills),
                           lambda r: build_run_argv(str(workdir), args.effort, r, brief))
    verdict = judge(this_round)

    # **撞上限只记一笔，不重跑。** v1~v3 在这里换账号重跑，安全论证是「撞上限的
    # 那一轮 codex 从未拿到响应，零工作量」——269 份现网日志、56 条真·额度错误行
    # 实测推翻了它：距本轮开头中位 101 行、最小 41 行，**0 条在轮首 12 行以内**。
    # 额度永远是在任务跑到一半用完的，而重跑用的是同一个 --dir、同一份 brief、
    # danger-full-access，落在一棵已经被改过的树上；而「这一轮到底改没改过文件」
    # 没法可靠地知道——唯一的线索是日志文本，而日志正是被污染的那个东西。
    # 于是「重试」从工具的一个循环，变成了调用方的一次重发，而重发是安全的：
    # 重发时 auto 已经知道这个账号满了。
    #
    # 判据不可靠这件事在这里的爆炸半径只有**记录**：误判最多给一个健康账号写条
    # 限流记录，于是它排到后面——仍然会被选中、仍然会被试，只是顺序差一格。
    if verdict.state == "failed" and hit_usage_limit(runtime_error_lines(this_round.text)):
        # 从**本轮的文本**解析，绝不另读整份历史日志——那会把上一轮、上一个任务
        # 的旧额度错误和旧恢复时间当成本轮事实。
        hit = parse_reset_time(this_round.text, now=datetime.datetime.now())
        if hit is None:
            记 = "日志里没有恢复时间可记（现网确有这种形态）"
        else:
            write_usage_limit(home, *hit)         # 写失败它自己吞掉并出声
            记 = f"恢复时间 {hit[0]:%m-%d %H:%M} 已记下"
        # 下一个账号**算出来，不写死**：记完这一笔之后重新排一次序，第一个就是
        # 下次会跑的那个。说「下次 auto 会先试」而不是「重跑这条命令会换成」，
        # 因为后者在两种情形下是假话：强制模式重跑的还是指定的那个账号；
        # 全部账号都有记录时第一个可能仍是它自己。
        print(f"[codex-sub-agent] {account} 撞上额度上限，{记}。"
              f"下次 --account {AUTO} 会先试 {accounts_by_availability()[0]}。")

    _print_verdict(args.task, verdict)
    print(f"  报告 {_report_path(home, args.task)}\n  日志 {_log_path(home, args.task)}")
    return EXIT[verdict.state]


def cmd_status(args):
    if args.task:
        home, meta = find_meta(args.task)
        if meta is None:
            reject(f"没有这个任务：{args.task}")
        rows = [(home, meta)]
    else:
        rows = all_metas()
    if not rows:
        print("还没有任何任务")
        return EXIT["success"]
    worst = "success"
    for home, meta in rows:
        report, log = _report_path(home, meta["task"]), _log_path(home, meta["task"])
        pid = find_codex_pid(report)
        # 还在跑就**不读日志**：判据在这一支根本用不到它，而 status 是轮询用的
        # 热路径。实测 read_last_round：0.83MB 约 10ms、8.3MB 约 100ms，
        # 列全部任务还要乘任务数。
        # 不在跑时它是外部观察者：手里没有本轮文本，只能看最后一轮，也只该看
        # 最后一轮。它**显式**构造 Round，所以「这是推测」在代码里看得见。
        verdict = (Verdict("running", f"pid={pid} 存活", []) if pid is not None
                   else judge(Round(report, read_last_round(log))))
        print(status_row(meta, verdict))
        for line in verdict.detail:
            # 缩进保持：首字符是空白 → 与数据行结构性可分，调用方不必猜哪行是任务
            print(f"    {line}")
        worst = _worse(worst, verdict.state)
    return EXIT[worst]


def check_can_resume(task, meta, brief_path):
    """续跑的四道闸。**只拒绝，不产生任何副作用**，所以可以在发信号之前先跑一遍。

    抽成独立函数，是因为 `interrupt-and-resume` 必须把**全部**拒绝跑在发信号
    之前：INT 发出去就收不回来，先打断、再发现没 session id，那一轮白毁**且拿
    不回来**（没 session id 就没法 resume）。这个窗口真实可达——session_id 要等
    codex 第一块输出才写进元数据，实测父进程 4.06 秒才看到 banner，而任务刚起
    那几秒正是最可能被打断的时候（刚发现 brief 写错）。

    `_resume_round` 自己也调它：闸留在续跑动作里，才没有一条绕过去的后门。
    两次调用是刻意的，纯拒绝、无副作用，跑两遍不花钱。

    **判据（已精化）：凡是对「调用方已经交给我们的输入」的纯检查，一律在任何
    不可逆动作之前做完。** 早先写的是「不可恢复的挪前面、可恢复的留后面」，
    而那条按字面会把第四道闸放到信号后面——它确实「可恢复」（上下文还在）。
    但那条判据的**本意**是「有些检查在动手之前做不了」（比如 `ensure_isolation`
    要先解析出账号），不是「可恢复就随便放」。brief 的内容是调用方交进来的输入，
    动手之前就问得出来；白白烧掉一轮，即使可恢复也是浪费。

    四道闸里前三道拒的是**不可恢复**的事：没 session id 就再也回不来，工作目录
    没了、brief 不是文件则连命令都拼不出来。第四道（brief 自带兜底句）是纯输入
    检查。四道都必须在 INT 发出去**之前**问清楚，因为 INT 收不回来。
    而 `_resume_round` 里的 `ensure_isolation` 确实会在信号**之后**才拒绝
    （实测：`.agents/skills` 非空时 `os.kill` 已经调过一次，随后退出码 2），
    它既可恢复、又**必须先解析出账号才做得了**，所以留在那儿没问题。
    **往这条路上加新拒绝时按这条判据放**：只要是对已经交进来的输入做纯检查，
    就挪到这里来。
    """
    if not meta["session_id"]:
        reject(f"任务 {task} 没有记到 session id，无法 resume，只能新起一个任务")
    workdir = pathlib.Path(meta["dir"])
    if not workdir.is_dir():
        reject(f"任务 {task} 的工作目录 {workdir} 不在了（worktree 被删？）。"
               f"codex 会以 os error 2 当场崩，所以这里直接拒。")
    brief_file = pathlib.Path(brief_path).expanduser()
    if not brief_file.is_file():
        reject(f"--brief {brief_path} 不是文件（brief 只收文件路径，避开引号地狱）")
    # 第四道：读一遍 brief 正文。`_resume_round` 随后还要再读一次（经
    # prepend_skill_guard），两次读同一个文件不花钱，换来的是「拒绝排在 INT 之前」。
    check_brief_has_no_guard(brief_file.read_text())


def _resume_round(kind, home, meta, task, brief_path, effort, skills):
    """两条路共用的续跑动作：`resume` 和 `interrupt-and-resume`。

    名字是 `_resume_round` 不是 `_resume_with`：`with` 没说清 with 什么，
    而它做的事就是「续跑一轮」（仓库规范第 2 条：不清晰的词换成清晰的词组）。

    它只管「已经确定停了之后怎么续」，**不判断该不该停**——`cmd_resume` 在调它
    之前拒绝还在跑的任务，`cmd_interrupt_and_resume` 在调它之前把它打断并确认
    退出。这条边界是刻意的：要不要停是调用方的判断，怎么续是工具的事。

    `kind` 进日志分隔符，所以日志里看得出这一轮是被插话打断后续上的。
    判据收的就是 `run_codex` 回传的本轮文本，**不用 read_last_round**：本轮的
    拥有者手里有事实，用最后一轮就是把事实换回推测（那正是轮次边界那次改动
    修掉的整类 bug）。

    `skills` 是本轮的白名单，和 effort 一样每轮重给——白名单是每一轮的事。
    """
    check_can_resume(task, meta, brief_path)
    ensure_isolation(meta["account"])
    brief = prepend_skill_guard(pathlib.Path(brief_path).expanduser().read_text(), skills)
    # 元数据描述的是**最后一次调用**：effort、白名单、开跑时间一起刷新。
    # 完整的轮次历史不在这里，在日志的分隔符里（每轮一行，带时间戳）。
    meta["effort"] = effort
    meta["skills"] = skills
    meta["started_at"] = _now_iso()
    verdict = judge(run_codex(kind, home, task, meta,
                              lambda r: build_resume_argv(meta["dir"], meta["session_id"],
                                                          effort, r, brief)))
    _print_verdict(task, verdict)
    return EXIT[verdict.state]


def cmd_resume(args):
    home, meta = find_meta(args.task)
    if meta is None:
        reject(f"没有这个任务：{args.task}")
    # resume 之前必须确认真的退出了：对还在跑的会话 resume，报的错和 SIGTERM 锁死
    # 一模一样（thread-store conflict），而处置完全相反——一个该等，一个该弃。
    # 本命令刻意**不替调用方打断**：要打断请用 interrupt-and-resume，
    # 那个名字把代价写在脸上。
    # **这道闸和 run_codex 里那个等待不是一回事，别合并。** 这里拒的是「上一轮
    # 还在正经跑」——那一轮还要跑多久没有上界，等它等于把调用方挂死，而且该不该
    # 打断是调用方的判断。run_codex 等的是「上一轮已经停了、只是还在收尾」，
    # 那是有限的、纯技术性的一小段，谁都不必为它做决定。
    #
    # 这里 PID 检查排在 check_can_resume **之前**，和 interrupt-and-resume 的闸序
    # 相反，是**刻意的**：「拒绝必须在动手之前」约束的是**副作用**，而这条路一个
    # 副作用都没有，闸序只决定人先看到哪句话——「还在跑」是这里最可操作的那句。
    # **不要为了对称把新命令的闸挪到信号后面**：那条路上 INT 发出去就收不回来。
    if find_codex_pid(_report_path(home, args.task)) is not None:
        reject(f"任务 {args.task} 还在跑，resume 会撞上它自己的写锁。"
               f"等它结束，或用 `codex-sub-agent interrupt-and-resume {args.task}`。")
    return _resume_round("resume", home, meta, args.task, args.brief, args.effort,
                         args.skills)


def cmd_interrupt_and_resume(args):
    """打断当前轮，然后用新消息续跑。

    它独有的收益有两条，**都和「等」无关**（等上一轮收尾已经是 run_codex 的事，
    三条路一视同仁）：

    ① **五道闸全部排在 INT 之前。** INT 发出去就收不回来，所以「该不该续跑」
       必须在动手之前问完（见 check_can_resume）。
    ② **挡第二发 INT。** 超时的处置是稍后重跑这条命令，而重跑会再走一遍这里；
       用本轮日志里已有的打断痕迹判，不发第二发（见下面那一支）。

    **`codex queue` 对 `codex exec` 完全无效**（2026-09-19 实测）：任务跑着时
    不收，resume 之后也不收，而且**静默成功、退出码 0**——比「不能用」更危险，
    照它写的调用方会以为消息送到了。所以「给正在跑的那一轮塞消息」没有别的路。

    **也刻意不做「排队等本轮自然结束再送达」**：粒度不对——长任务上，等它跑完
    再告诉它「方向错了」，等于让它把错的方向跑到底再返工；而这条命令存在的场景
    恰恰是「刚发现 brief 写错」。那还要引入一个 pending 队列的新实体，
    而队列一旦存在就得回答「没送达之前任务结束了怎么办」。

    **要不要为此打断，仍然是调用方的判断**：那要知道「这条信息值多少」和「在途
    工作损失多少」，后者在 codex 里根本不可观测。命令名把代价写在脸上，
    工具不替谁做这个决定。

    下面的顺序是**硬约束**，不是排版顺序：五道闸全部走完才允许发信号。
    """
    # ────── 五道闸 ──────
    home, meta = find_meta(args.task)                      # 1. 任务存在？
    if meta is None:
        reject(f"没有这个任务：{args.task}")
    check_can_resume(args.task, meta, args.brief)   # 2/3/4/5. session id / 目录 / brief 是文件 / brief 不自带兜底句
    # ────── 以上全过，才允许动手 ──────

    # meta 在这里读一次就一直用到 _resume_round，中途可能旧于磁盘上那份（被打断
    # 的那一轮会在收尾时写元数据），但无损：差异字段只有 session_id（为空早被
    # 上面的闸拒了）、effort／started_at（本来就要刷新），以及 writer_pid／
    # writer_start——后两个由 run_codex 临落盘前现盖，这里拿的是哪一份都不影响。
    report, log = _report_path(home, args.task), _log_path(home, args.task)
    pid = find_codex_pid(report)
    if pid is None:
        # 两种入场都要吃：调用方无法可靠知道自己在哪一种——查完到动手之间，
        # 任务可能刚好跑完。所以两条都走通，并如实说走了哪条。
        print(f"[codex-sub-agent] {args.task} 本来就没在跑，直接续跑")
    else:
        # 超时的处置是「稍后重试」，而重试就是再跑一遍这条命令——不加这道判断，
        # 重试就会发出**第二发 INT**。很多 CLI 把第二发 Ctrl-C 当强退，codex
        # 是不是这样完全没验过；如果是，就走成不干净退出 → 写锁不释放 →
        # 上下文全丢，正是本命令要防的事。
        # 用已有的痕迹判，不加新实体。外部观察者只能看最后一轮，而它要问的
        # 恰好就是最后一轮的事。
        if has_interrupt_mark(read_last_round(log)):
            print(f"[codex-sub-agent] {args.task} 本轮已经打断过（pid={pid} 还在收尾），"
                  f"只等它退出，不再发第二发 INT")
        else:
            interrupt_codex(pid, log, "interrupt-and-resume")
            print(f"[codex-sub-agent] {args.task} 还在跑（pid={pid}），已发 SIGINT 并在日志留痕")
    # **本命令的退出码＝续跑那一轮的判据结论**（0/1/3/130），不是「打断成功没」。
    # 打断只是手段，调用方要的是「新消息跑出什么结果」；而护栏拒绝走 2，
    # 与判据结论不撞码，所以这两件事在退出码上始终分得开。
    return _resume_round("interrupt-and-resume", home, meta, args.task, args.brief,
                         args.effort, args.skills)


def cmd_stop(args):
    home, meta = find_meta(args.task)
    if meta is None:
        reject(f"没有这个任务：{args.task}")
    pid = find_codex_pid(_report_path(home, args.task))
    if pid is None:
        print(f"任务 {args.task} 已经不在跑了")
        return EXIT["success"]
    # 发 INT 与留痕焊在 interrupt_codex 里，这条路不可能只做一半。
    # 上面 `pid is None` 那一支正是「调用之前判它在不在跑」的地方，
    # 所以 interrupt_codex 不需要回一个 bool 让这里再判一遍。
    interrupt_codex(pid, _log_path(home, args.task), "stop")
    print(f"已向 {args.task} (pid={pid}) 发 SIGINT，上下文保留，可 resume")
    return EXIT["success"]


class _AppendSkillPath(argparse.Action):
    """`--skill` 往 **tuple** 上拼，不用现成的 `action="append"`。

    argparse 那两件现成零件各有一处毛病，凑在一起就是两条软约定：
      - `action="append"` 给 list，而 `--no-skill` 的 `const=` 给的是**同一个
        对象**（跨 parse 共享）。给 `[]` 的话谁原地改一下就污染另一次 parse。
      - 两支类型还不一样，于是 `args.skills == []` 在一支上成立、另一支上踩空。
    统一成不可变的 tuple 之后，「别原地改」和「别拿 `== []` 比」都不再需要
    有人记得——前者结构上做不到，后者两支一致地为 False（要判空写
    `if not args.skills`）。
    """

    # `option_string=None` 是 **argparse 的 Action 契约签名**，不是本仓禁止的那种
    # 默认缺省值——argparse 调它时按位置传前三个、`option_string` 用关键字传，
    # 签名少一个默认值就 TypeError。别当违规删掉。
    def __call__(self, parser, namespace, value, option_string=None):
        setattr(namespace, self.dest, (getattr(namespace, self.dest) or ()) + (value,))


def _add_prompt_round_args(sub):
    """带 prompt 的三条命令共用的「本轮 skill 白名单」。**二选一必填，没有默认值。**

    白名单是**每一轮**的事，不是任务的事——resume 换一轮活，能用的 skill 就该
    跟着换，所以三条命令各收一份，而不是在 run 时定死。

    为什么 `--no-skill` 必须显式写：这个工具要交给其他 agent 用。省略时分不清
    「调用方决定不给」和「调用方根本不知道有这个参数」，而 argparse 的
    `required=True` 能把后者变成 exit 2 当场报错。代价很小——这个拒绝是即时且
    完全可恢复的（加个参数重跑，零损失），不像 --effort/--account 写错要花钱
    才发现。

    **刻意不把 skill 软链进隔离目录**，理由是可观测性：路径在 brief 里、**而且
    兜底句明文叫它去读**（见 build_skill_guard——那句指令就是这条论证的地基，
    光给许可不给指令的话「读没读」根本无从观测），于是「codex 到底读没读」在
    日志里看得见，就是那一行 `cat <路径>`；软链成能力之后，用没用由它决定、
    **不可观测**。对一个主张「不骗调用方」的工具，这条是决定性的。第二条是奥卡姆：软链要求隔离目录从「每账号一个」变成「每任务
    一个」，isolation_home／find_meta／account_choices／ensure_isolation 全线
    要改，换来的保证是零。
    """
    # 两支都给**不可变的 tuple**，所以 args.skills 的契约就一句话：
    # 恒为 `tuple[str]`，`--no-skill` 时为空。理由见 _AppendSkillPath。
    # 序列化不受影响：json.dumps(()) 就是 []，元数据的形状一个字没变。
    g = sub.add_mutually_exclusive_group(required=True)
    g.add_argument("--skill", action=_AppendSkillPath, dest="skills", type=skill_path,
                   metavar="SKILL_MD", help="允许 codex 读的 SKILL.md 绝对路径，可重复")
    g.add_argument("--no-skill", action="store_const", const=(), dest="skills",
                   help="本轮一个 skill 都不给")


def build_parser():
    """命令行契约。

    整个工具选 Python3 写，理由就在这个函数里：`argparse` 的 `required=True`
    + `choices=` + 互斥组天然实现了「强制显式」——五个带值参数一个都不能少、
    `--skill`/`--no-skill` 二选一、难度和账号只能从枚举里挑，而且**错误消息是
    免费的**，不用自己写一遍校验和提示。
    另一半理由在判据那边：那些是纯函数，单测跑一遍零 codex token。
    """
    p = argparse.ArgumentParser(
        prog="codex-sub-agent",
        description="把执行类任务派给 codex 后台跑。用 Bash(run_in_background: true) 启动 run。")
    sub = p.add_subparsers(dest="cmd", required=True)

    # 五个带值参数全必填，外加 --skill/--no-skill 二选一：不设默认值，因为隐式
    # 选中的账号／难度／白名单都是最容易被误用的地方
    r = sub.add_parser("run", help="起一个新任务")
    r.add_argument("--task", required=True, type=task_name,
                   help="任务名，全局唯一（PID 反查和产物命名都靠它）")
    r.add_argument("--dir", required=True, type=work_dir, help="codex 的工作目录，自动转绝对路径")
    r.add_argument("--brief", required=True, help="brief 文件路径（只收文件，不收内联字符串）")
    r.add_argument("--effort", required=True, choices=EFFORTS, help="难度分档")
    # `--account` **仍然必填**：没有任何隐式选中的值。取值只是多了一个 AUTO。
    r.add_argument("--account", required=True, choices=account_choices() + [AUTO],
                   help=f"codex 账号；{AUTO} = 自动挑一个没在限流的，撞上额度上限就换下一个")
    _add_prompt_round_args(r)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="看任务状态；省略任务名则列出全部")
    s.add_argument("task", nargs="?", type=task_name)
    s.set_defaults(func=cmd_status)

    # resume/stop 不收 --account：账号从元数据查出来，不可能指错
    m = sub.add_parser("resume", help="给已结束的任务补一轮")
    m.add_argument("task", type=task_name)
    m.add_argument("--brief", required=True)
    m.add_argument("--effort", required=True, choices=EFFORTS)
    _add_prompt_round_args(m)
    m.set_defaults(func=cmd_resume)

    # 名字刻意长而直白：它会**截断当前轮**，这个代价必须写在脸上。
    # 仓库规范第 2 条说「不清晰的单词全部换为简单清晰的词组」——
    # `interject` 是个不清晰的单词，`interrupt-and-resume` 是个清晰的词组。
    # 参数表与 resume 逐条一致：同一个工具里 prompt 只有一种传法。
    # help= 进父 parser 的子命令列表，description= 进它自己的 --help。
    # 只传 help= 的话 `interrupt-and-resume --help` 里看不到「会截断当前轮」
    # （实测 grep -c 截断 = 0），而那正是这条命令最该被看见的代价。
    _iar_help = "打断当前轮并用新消息续跑（会截断当前轮，上下文保留）"
    j = sub.add_parser("interrupt-and-resume", help=_iar_help, description=_iar_help)
    j.add_argument("task", type=task_name)
    j.add_argument("--brief", required=True)
    j.add_argument("--effort", required=True, choices=EFFORTS)
    _add_prompt_round_args(j)
    j.set_defaults(func=cmd_interrupt_and_resume)

    k = sub.add_parser("stop", help="停一个任务（只发 SIGINT）")
    k.add_argument("task", type=task_name)
    k.set_defaults(func=cmd_stop)
    return p


def main():
    """入口。

    **「必须用 `Bash(run_in_background: true)` 启动」这一条编不进硬约束**，
    2026-09-19 实测过为什么：前台和后台两种方式下，本进程看到的环境**逐字节相同**
    ——14 个 `CLAUDE_*` 变量、父进程、tty 状态全一样。没有任何信号能让本进程判断
    自己是不是跑在 harness 的追踪之下，所以这条只能留在 SKILL.md 当软约定。

    能做的是把它的**灾难性后果**消掉，那已经做了：前台跑被 2 分钟超时杀掉时，
    包装器把收到的信号统一转成 INT 再转发（见 run_codex），codex 的上下文保住、
    仍可 resume，日志里还留下一行带来源的打断标记（`[外部信号转发]`，见
    interrupt_codex）告诉下一个人该 resume 而不是重跑。于是误用的代价从
    「会话永久锁死、上下文全丢」降到「这一轮没拿到完成通知」——可恢复。

    **这条路上别指望退出码。** 链路是「harness 超时 → TERM 打进程组 → codex
    收不到 → 包装器转 INT → 留痕 → 判据 interrupted → 返回 130」，而此时
    harness 已经超时了，它报的是超时，未必会去收那个 130。
    退出码 130 是 `run_in_background` **正常路径**上的通道；前台误跑这条路上
    真正可靠的是**日志里那行痕迹**（连带它的来源），以及之后任何一次 `status`
    的复述。

    **这也是为什么本工具是 CLI 而不是 MCP server**（同一件事的另一面）。
    这套东西的价值有三条：① codex 干活 ② claude 不等、去干别的 ③ 干完自动通知。
    MCP 的两种形态各杀掉一条：
      - 同步等待 codex 结束 —— 任务动辄几十分钟，要么超时、要么把 claude
        阻塞在原地，②没了；
      - 立即返回 + 轮询 —— 完成通知来自 **harness 对 `Bash(run_in_background)`
        的追踪**，MCP 没有这个通道，③没了。
    MCP 能买到的只有「参数有 schema」和「不用读长 skill」，而 argparse 的
    `required=True` + `choices=` 同样给得到，还不丢后台通道。
    （附：codex 0.154.0 的 `codex mcp` 只是管理**外部** MCP server，
    它不提供「把自己暴露成 MCP server」这回事。）

    **同一个问题的另一半：为什么不换传输层。** `codex app-server` / daemon 那条
    路确实做得到本工具做不到的事——在**工具边界**插话，不必截断当前轮；socket
    路径由 `CODEX_HOME` 派生，所以起一个私有 daemon 不会破坏隔离，技术上可行。
    不走它的理由只有一条，但够硬：**那是 `[experimental]` 协议**，而本仓已经被
    版本漂移教训过**三次**——`--sandbox` 在 resume 上不认、`--color` 在 resume
    上不认（整轮当场死）、`codex queue` 对 exec 静默无效。
    稳定 CLI 上的漂移都有这个量级，押在实验性协议上等于把这个工具的全部保证
    挂在一个不承诺兼容的接口上。等它脱离 experimental 再谈。
    """
    # stdout 接管道／文件时文本层默认**块缓冲**，而这条 fd 有两个写者：
    # 本模块的 print 走文本层，run_codex 的 tee 走 sys.stdout.buffer（自己 flush）。
    # 不改成行缓冲，包装器「此刻正在发生什么」的话会排到 codex 整轮输出之后
    # （2026-09-19 端到端实测拿到过这个错序：`已发 SIGINT` 那句落在最后面）。
    # 最吃这条的是 `_wait_previous_round_ends` 那句「上一轮还在收尾，最多等 N 秒」：
    # 它排在本轮分隔符**之前**，日志里此刻什么都没有，屏幕再不出声就是个静默挂起
    # ——**那句话当场看得见，正是这一行行缓冲买到的东西**。
    #
    # **在这里改一次**，而不是每个 print 加 flush=True，也不是收一个 _say()——
    # 那两种都是软约定：15 个调用点都得记得用对的那个，漏一个就静默错序，
    # 而漏一个不会报错。实测那两个突变（新加一行裸 print、把某句 _say 改回裸
    # print）在收了 _say() 的版本上**都存活**。行缓冲之后裸 print 自动正确。
    sys.stdout.reconfigure(line_buffering=True)
    # build_parser() 也在 try 里面：account_choices() 扫到坏账号目录名时会 reject，
    # 而 Rejected 就是 SystemExit(2)——漏在 try 外面它会直接逃出去，退出码还是 2
    # 但 message 全丢，调用方拿到一个没有任何解释的 2。对 agent 调用方，
    # 「有码无话」是最救不回来的一种失败。
    # argparse 自己的参数错误不是 Rejected（它自己已经把话写到 stderr 了），
    # 照旧原样逃出去，这里不拦。
    try:
        args = build_parser().parse_args()
        return args.func(args)
    except Rejected as e:
        print(e.message, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
