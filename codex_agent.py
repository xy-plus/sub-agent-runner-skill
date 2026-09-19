#!/usr/bin/env python3
"""codex-agent —— 把 `codex exec` 的实测约束编译成硬约束的包装器。

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


def _reject_control_chars(flag, value):
    """控制字符必须在**入口**挡住，不能指望「一般没人这么干」。

    2026-09-19 实测：`--dir` 给一个含制表符和换行的路径，`mkdir`／`resolve()`／
    `is_dir()` **全都放行**——文件系统这一层根本不管。而这个路径会原样进
    `status` 的数据行，一个换行就让「一行一任务」不成立，任何切分方案都救不
    回来（见 status_row）。
    `--skill` 同理，但坏法不同：兜底句把白名单**逐行**列出来，路径里一个换行
    就把一条白名单静默劈成两行，codex 读到的是两个都不存在的路径。
    """
    hit = _CONTROL_CHARS.search(value)
    if hit:
        # 这里用 !r：value 已经确定含控制字符，裸插进错误信息会把 stderr 也弄成
        # 多行／带制表符的一坨。skill_path 后面三条的 value 是干净路径，用裸的。
        raise argparse.ArgumentTypeError(
            f"{flag} {value!r} 含控制字符 {hit.group()!r}（第 {hit.start()} 个字符）。"
            f"这个值要原样进 status 的数据行和兜底句的白名单行，控制字符会把它们切坏。")


def work_dir(value):
    """`--dir` 的 type=。只管控制字符；「是不是目录」归 cmd_run——它要先 expanduser／resolve。"""
    _reject_control_chars("--dir", value)
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
    _reject_control_chars("--skill", value)
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
    return value


USAGE_LIMIT_MARK = "You've hit your usage limit"
THREAD_LOCK_MARK = "already has an active writer"
REPORT_PREVIEW_LINES = 5

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

ROUND_MARK = "===== codex-agent "   # 每轮开跑前写进日志的分隔符前缀

# 本轮被信号打断时留在日志里的痕迹。刻意不以 ROUND_MARK 开头——不过这条现在
# 不再是靠人记住的前缀约定了：两个识别器都整行匹配（见下），互相不可能命中。
INTERRUPT_MARK = "----- codex-agent 本轮被 INT 打断，上下文保留，可 resume -----"


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
# codex_agent.py 自己。源码里那两行常量定义一旦被转述进日志，**两个方向都翻车**：
#   日志里出现 `ROUND_MARK = "===== codex-agent "` 这行源码
#     → 本轮文本在那里被切断（实测切剩 'ROUND_MARK = "' 共 14 个字符）
#     → 打断标记被甩到本轮之外 → judge 从 interrupted 翻成 **failed**
#   日志里出现 `INTERRUPT_MARK = "----- codex-agent 本轮被 INT 打断…"` 这行源码
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
        # 两种特判只改 reason、不新增状态：补救手段不同（换账号／新起任务），
        # 但都属于「没正常收尾」这一种事实，状态机不该为此变复杂。
        if USAGE_LIMIT_MARK in round_text:
            return Verdict("failed", "撞上账号额度上限，换账号或等额度恢复", errors)
        if THREAD_LOCK_MARK in round_text:
            return Verdict("failed",
                           "会话被写锁占住（上一轮没真的结束，或曾被 SIGTERM 杀过），只能新起一个任务",
                           errors)
        # 排在额度上限和写锁之后：那两条意味着 resume 也救不回来（换账号／新起
        # 任务），而这一条恰恰是「resume 就行」，不能把更坏的消息盖掉。
        # 日志里同时有 codex_core::session 的错误行是常态（被 INT 打断几乎必然
        # 留下 failed to record rollout items），那些照常进 detail，不改状态。
        if has_interrupt_mark(round_text):
            return Verdict("interrupted",
                           "本轮被 INT 打断，上下文保留——接着 resume 即可，不用重跑", errors)
        return Verdict("failed", "报告缺失或为空＝没正常收尾", errors)

    if errors:
        return Verdict("suspect", f"报告在，但本轮日志有 {len(errors)} 条未分类的 codex 错误", errors)

    # 报告内容由 brief 决定（要 commit 还是要别的），属于任务层不属于工具层。
    # 只预览前几行，让调用方自己核对 brief 要的东西在不在——不解析 JSON：实测
    # 156 份真实报告只有 4 份是 JSON，`-o` 写的是 agent 的最后一条消息，通常是
    # markdown 散文。要结构化输出那是 --output-schema 的事。
    preview = [l for l in report_text.splitlines() if l.strip()][:REPORT_PREVIEW_LINES]
    return Verdict("success", "正常收尾，本轮日志无未分类错误", preview)


MODEL = "gpt-6-astra"

# 隔离目录自己的 config，绝不软链主配置。
# 2026 年踩过：`codex-acct` 把 config.toml 软链到主配置，一用就把 MCP、plugins、
# hooks、memories 全带回来，隔离当场失效。账号和隔离是正交的两件事，要组合。
#
# 这份初始内容**刻意只有注释**。model／effort／sandbox_mode／approval_policy
# 由 CLI 每次显式传，写进 config 就是同一条事实有两个家，还是个会被静默覆盖的
# 缺省值（现存两个隔离目录的 model/effort/service_tier 本来就互相打架）。
CONFIG_NOTE = '''# codex-agent 的隔离配置。
# 这个文件必须是本目录自己的普通文件，不许软链 ~/.codex/config.toml——
# 软链会把主配置的 MCP／plugins／hooks／memories 全带回来，隔离当场失效。
# 刻意不写 model / model_reasoning_effort / sandbox_mode / approval_policy：
# 那些由 codex-agent 每次运行显式传参，写在这里只会变成一份会被静默覆盖的缺省值。
# codex 自己会往下面追加 [projects.*] trust_level，那是它的状态，不要手动清。
'''


def account_choices():
    """账号可选项由实际目录扫描得出，不硬编码——加了账号就自动认。"""
    accounts_dir = pathlib.Path.home() / ".codex-accounts"
    extra = sorted(p.name for p in accounts_dir.iterdir() if p.is_dir()) if accounts_dir.is_dir() else []
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
    """保证隔离目录满足全部不变量，不满足就拒跑（而不是“尽力而为”地继续）。"""
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
# 「**不得使用任何 skill，除非本 brief 明确指定。**」也含有它——所以拿它当
# 「调用方是不是自己写了兜底句」的判据，一条就挡住全部写法。
SKILL_GUARD_STEM = "不得使用任何 skill"

# 主线和 resume 都固定带上的参数。调用方碰不到它们，也就不可能漏掉。
# `--color never` **不在这里**：resume 不认它（见 build_resume_argv）。
_COMMON = ["-c", "approval_policy=\"never\"", "-c", "project_doc_max_bytes=0",
           "--skip-git-repo-check", "--disable", "plugins"]


def build_skill_guard(skill_paths):
    """本轮的兜底句。**每轮派生，不是常量**——白名单是每一轮的事。

    旧常量那半句「除非本 brief 明确指定」本来就是「CLI 没有这个参数」的变通：
    调用方无处声明白名单，只好让 brief 正文去破例。`--skill` 出现之后那半句就
    该消失——白名单由 CLI 指定，brief 正文不再是声明渠道。

    白名单**逐行**列出，所以路径里一个换行就能把一条静默劈成两条
    （见 _reject_control_chars，那条拒绝就是为这里守的）。
    """
    if not skill_paths:
        return f"**{SKILL_GUARD_STEM}。**"
    return f"**{SKILL_GUARD_STEM}，以下几个除外：**\n" + "\n".join(f"- {q}" for q in skill_paths)


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
    这次要认的是**调用方写了任意措辞的兜底句**，误判的后果是拒绝一个确实在谈
    skill 禁令的 brief——而那正是我们要拒的。失败方向是良性的。
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
    meta_path(home, task).write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def new_meta(task, account, workdir, effort):
    """元数据的**唯一**构造器。字段清单只在这里写一次。

    刻意没有 pid：存活必须每次重新反查，存下来的 PID 会过期、还会被系统复用，
    留着它只会诱导别人犯这个设计本来要防的错。
    """
    return {"task": task, "account": account, "dir": workdir, "effort": effort,
            "session_id": None, "started_at": _now_iso()}


# 校验面由构造器派生，**不另写一份清单**。两份清单必然漂移，而漂移的后果是
# 静默的：少一个字段，run 照常报成败，但那个任务从此 status/resume/stop 全
# 够不着，工具还会建议「删掉它重新 run」——会话就此丢掉。
# 读回来就校验，之后所有地方放心裸下标；`.get(键, 默认值)` 是默认缺省值，
# 正是本工具要消灭的东西。
REQUIRED_META_KEYS = tuple(new_meta("", "", "", "").keys())


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
    # 不做这一下，wait_for_exit 就会变成全模块唯一收 str 的函数——同一个东西两种
    # 传法，正是「好 API 难被误用」要消掉的那种缝。
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


# 打断之后最多等它收尾这么久。**是上界不是等待时长**——一确认退出就立刻往下走。
# 实测锚点（2026-09-19，codex 0.154.0，effort low，sleep 工具调用执行中被打断）：
# INT → PID 消失分别是 **1.854 秒**和 **0.964 秒**。60 秒是 30~60 倍余量——
# 够大到不会误杀正常收尾，够小到卡住时调用方不会被无限期挂着。
# 两个数字写在这里，是因为没有它们下一个人会随手改这个 60。
#
# 必须是**模块级常量**而不是函数默认参数：测试要把它 patch 成 0.3 秒；
# 也正合仓库规范「不要默认缺省值」——调用方每次都显式传，不可能漏。
INTERRUPT_EXIT_TIMEOUT = 60
INTERRUPT_POLL_INTERVAL = 0.2


def wait_for_exit(report_path, timeout, poll_interval):
    """轮询真实 PID 直到它真的退出。返回是否在 timeout 之内退出。

    `report_path` 是 `pathlib.Path`，和本模块其它地方一致（见 find_codex_pid）。

    这是 `interrupt-and-resume` **唯一独有的收益**：护栏只会拒绝，不会替你等。
    `cmd_resume` 早就拦住了「对还在跑的会话 resume」（实测 stop 之后 0.164 秒
    resume，拿到的是干净的 exit 2 拒绝，不是 thread-store conflict），但调用方
    拿到 exit 2 之后得自己写重试循环——间隔多少、上界多少、超时了怎么办，全是
    软约定，每个调用方现编一遍，编错了没人告诉他。把这个循环收进来就是它存在
    的全部理由。

    存活判据与 `status` 同一套（扫 /proc + argv 元素精确比对 + comm + 同用户），
    不另起一份——两份判据必然漂移。

    **只轮询，不发任何信号。** 超时的正确处置是让调用方稍后再来，不是加大火力：
    升级到 SIGTERM 会让会话永久锁死，而那一步不可逆。
    """
    deadline = time.monotonic() + timeout
    while True:
        if find_codex_pid(report_path) is None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


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
# 悄悄落成 0，`codex-agent status t && deploy` 就会在任务还在跑的时候部署。
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


def _print_verdict(task, verdict):
    print(f"\n[codex-agent] {task}: {verdict.state} —— {verdict.reason}")
    for line in verdict.detail:
        print(f"  {line}")


def cmd_run(args):
    workdir = pathlib.Path(args.dir).expanduser().resolve()
    if not workdir.is_dir():
        reject(f"--dir {args.dir} 不是目录")
    brief_file = pathlib.Path(args.brief).expanduser()
    if not brief_file.is_file():
        reject(f"--brief {args.brief} 不是文件（brief 只收文件路径，避开引号地狱）")

    home = isolation_home(args.account)
    old_home, old_meta = find_meta(args.task)
    if old_meta is not None:
        # 换账号重跑同名任务很好走（撞额度上限时就该这么干），但那会让同一个名字
        # 出现在两个隔离目录里：find_meta 按账号顺序查，另一份就成了再也够不着的
        # 孤儿元数据，而「上一轮会被覆盖」那句提示在跨账号时还是假话（报告路径不同）。
        if old_home != home:
            reject(f"任务名 {args.task} 已经属于账号 {old_meta['account']}（{old_home}）。\n"
                   f"同名任务跨账号会让 status/resume/stop 指向哪个变得不确定，换个任务名。")
        if find_codex_pid(_report_path(home, args.task)) is not None:
            reject(f"任务名 {args.task} 还在跑，换个名字或先 `codex-agent stop {args.task}`")
        print(f"[codex-agent] 提示：任务名 {args.task} 复用，上一轮的报告会被删掉、日志会被追加")

    ensure_isolation(args.account)
    report = _report_path(home, args.task)
    brief = prepend_skill_guard(brief_file.read_text(), args.skills)
    # 不再打印兜底句：它每轮派生、有白名单时是多行，而「这一轮给了哪些 skill」
    # 的权威副本在元数据的 skills 字段里（见 new_meta）。印第二份只会漂移。

    # 本轮的拥有者：judge 收的就是 run_codex 回传的那一对，**不用 read_last_round**
    # ——后者是外部观察者的上界，拥有者用它就是把事实换回推测。
    verdict = judge(run_codex("run", home, args.task,
                              new_meta(args.task, args.account, str(workdir), args.effort),
                              lambda r: build_run_argv(str(workdir), args.effort, r, brief)))
    _print_verdict(args.task, verdict)
    print(f"  报告 {report}\n  日志 {_log_path(home, args.task)}")
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
        print(f"{meta['task']:<24} {meta['account']:<8} {verdict.state:<8} "
              f"{verdict.reason}  {meta['dir']}")
        for line in verdict.detail:
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
    # 元数据描述的是**最后一次调用**：effort 和开跑时间都刷新。
    # 完整的轮次历史不在这里，在日志的分隔符里（每轮一行，带时间戳）。
    meta["effort"] = effort
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
    #
    # 这里 PID 检查排在 check_can_resume **之前**，和 interrupt-and-resume 的闸序
    # 相反，是**刻意的**：「拒绝必须在动手之前」约束的是**副作用**，而这条路一个
    # 副作用都没有，闸序只决定人先看到哪句话——「还在跑」是这里最可操作的那句。
    # **不要为了对称把新命令的闸挪到信号后面**：那条路上 INT 发出去就收不回来。
    if find_codex_pid(_report_path(home, args.task)) is not None:
        reject(f"任务 {args.task} 还在跑，resume 会撞上它自己的写锁。"
               f"等它结束，或用 `codex-agent interrupt-and-resume {args.task}`。")
    return _resume_round("resume", home, meta, args.task, args.brief, args.effort,
                         args.skills)


def cmd_interrupt_and_resume(args):
    """打断当前轮 + 确认它真的退出了 + 用新消息续跑，三件事不可分。

    它唯一独有的收益：**护栏只会拒绝，不会替你等。** `cmd_resume` 早就拦住了
    「对还在跑的会话 resume」（实测 stop 之后 0.164 秒 resume，拿到的是干净的
    exit 2 拒绝，不是 thread-store conflict），但调用方拿到 exit 2 之后得自己
    写重试循环——间隔多少、上界多少、超时了怎么办，全是软约定，每个调用方现编
    一遍，编错了没人告诉他。收走这个循环就是这条命令存在的全部理由。

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

    # meta 在这里读一次就一直用到 _resume_round。wait_for_exit 之后它已经旧于
    # 磁盘上那份（被打断的那一轮会在收尾时写元数据），但无损：差异字段只有
    # session_id（为空早被上面的闸拒了）和 effort／started_at（本来就要刷新）。
    report, log = _report_path(home, args.task), _log_path(home, args.task)
    pid = find_codex_pid(report)
    if pid is None:
        # 两种入场都要吃：调用方无法可靠知道自己在哪一种——查完到动手之间，
        # 任务可能刚好跑完。所以两条都走通，并如实说走了哪条。
        print(f"[codex-agent] {args.task} 本来就没在跑，直接续跑")
    else:
        # 超时的处置是「稍后重试」，而重试就是再跑一遍这条命令——不加这道判断，
        # 重试就会发出**第二发 INT**。很多 CLI 把第二发 Ctrl-C 当强退，codex
        # 是不是这样完全没验过；如果是，就走成不干净退出 → 写锁不释放 →
        # 上下文全丢，正是本命令要防的事。
        # 用已有的痕迹判，不加新实体。外部观察者只能看最后一轮，而它要问的
        # 恰好就是最后一轮的事。
        if has_interrupt_mark(read_last_round(log)):
            print(f"[codex-agent] {args.task} 本轮已经打断过（pid={pid} 还在收尾），"
                  f"只等它退出，不再发第二发 INT")
        else:
            interrupt_codex(pid, log, "interrupt-and-resume")
            print(f"[codex-agent] {args.task} 还在跑（pid={pid}），已发 SIGINT 并在日志留痕")
        if not wait_for_exit(report, INTERRUPT_EXIT_TIMEOUT, INTERRUPT_POLL_INTERVAL):
            reject(f"任务 {args.task} 收到 INT 后 {INTERRUPT_EXIT_TIMEOUT} 秒还没退出，"
                   f"还在收尾。稍后重跑这条命令即可——它不会再发第二发 INT。"
                   f"绝不升级信号：SIGTERM 会让会话永久锁死，不可逆。")
        print("[codex-agent] 已确认退出，本轮被提前结束——已做的部分留在上下文里")
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


def _add_prompt_round_args(sub):
    """带 prompt 的三条命令共用的「本轮 skill 白名单」。**二选一必填，没有默认值。**

    白名单是**每一轮**的事，不是任务的事——resume 换一轮活，能用的 skill 就该
    跟着换，所以三条命令各收一份，而不是在 run 时定死。

    为什么 `--no-skill` 必须显式写：这个工具要交给其他 agent 用。省略时分不清
    「调用方决定不给」和「调用方根本不知道有这个参数」，而 argparse 的
    `required=True` 能把后者变成 exit 2 当场报错。代价很小——这个拒绝是即时且
    完全可恢复的（加个参数重跑，零损失），不像 --effort/--account 写错要花钱
    才发现。

    **刻意不把 skill 软链进隔离目录**，理由是可观测性：路径在 brief 里，
    「codex 到底读没读」在日志里看得见（就是那一行 `cat <路径>`）；软链成能力
    之后，用没用由它决定、**不可观测**。对一个主张「不骗调用方」的工具，这条是
    决定性的。第二条是奥卡姆：软链要求隔离目录从「每账号一个」变成「每任务
    一个」，isolation_home／find_meta／account_choices／ensure_isolation 全线
    要改，换来的保证是零。
    """
    g = sub.add_mutually_exclusive_group(required=True)
    g.add_argument("--skill", action="append", dest="skills", type=skill_path,
                   metavar="SKILL_MD", help="允许 codex 读的 SKILL.md 绝对路径，可重复")
    g.add_argument("--no-skill", action="store_const", const=[], dest="skills",
                   help="本轮一个 skill 都不给")


def build_parser():
    """命令行契约。

    整个工具选 Python3 写，理由就在这个函数里：`argparse` 的 `required=True`
    + `choices=` 天然实现了「强制显式」——五个参数一个都不能少、难度和账号只能
    从枚举里挑，而且**错误消息是免费的**，不用自己写一遍校验和提示。
    另一半理由在判据那边：那些是纯函数，单测跑一遍零 codex token。
    """
    p = argparse.ArgumentParser(
        prog="codex-agent",
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
    r.add_argument("--account", required=True, choices=account_choices(), help="codex 账号")
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
    # （2026-09-19 端到端实测拿到过这个错序：`已发 SIGINT`、`已确认退出` 两句
    # 都落在最后面，而 wait_for_exit 卡住的那 60 秒里屏幕上一个字都没有）。
    #
    # **在这里改一次**，而不是每个 print 加 flush=True，也不是收一个 _say()——
    # 那两种都是软约定：15 个调用点都得记得用对的那个，漏一个就静默错序，
    # 而漏一个不会报错。实测那两个突变（新加一行裸 print、把某句 _say 改回裸
    # print）在收了 _say() 的版本上**都存活**。行缓冲之后裸 print 自动正确。
    sys.stdout.reconfigure(line_buffering=True)
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except Rejected as e:
        print(e.message, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
