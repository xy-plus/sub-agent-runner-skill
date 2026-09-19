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


# 任务名同时是文件名和 pgrep 的匹配模式，两边都会被奇怪字符咬：
#   `a/b`   写不出文件（裸 FileNotFoundError）
#   `../x`  写到 tasks/ 外面去
#   `a|b`   在 `pgrep -f` 里是**正则的或**，会命中任意含 `b` 的进程——
#           于是 stop 把 SIGINT 发到别人的 codex 上，正是 spec §9 发誓要避免的事
# 放在 argparse 的 type= 上，四个子命令一个都绕不过去。
_TASK_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def task_name(value):
    if not _TASK_NAME.match(value):
        raise argparse.ArgumentTypeError(
            "只允许字母、数字、点、下划线、连字符（任务名既是文件名，也是 pgrep 的匹配模式）")
    return value


USAGE_LIMIT_MARK = "You've hit your usage limit"
THREAD_LOCK_MARK = "already has an active writer"
REPORT_PREVIEW_LINES = 5

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

ROUND_MARK = "===== codex-agent "   # 每轮开跑前写进日志的分隔符前缀

# 本轮被信号打断时留在日志里的痕迹。**刻意不以 ROUND_MARK 开头**：
# current_round 是按 ROUND_MARK 往回切的，这行要是同前缀，它就会被当成新一轮
# 的开始，本轮前面的错误全被丢掉，判据当场失明。
INTERRUPT_MARK = "----- codex-agent 本轮被 INT 打断，上下文保留，可 resume -----"


def round_separator(kind, task, when_iso):
    return f"{ROUND_MARK}{kind} {task} {when_iso} ====="


def note_interrupt(log_path):
    """在信号处理器里往日志追一行打断标记。

    为什么值得为它多写一个函数：把 TERM 转成 INT 保住了上下文，却没人告诉
    下一个读判据的人「这轮是被打断的」。2026-09-19 真机复现过——`stop t` 刚
    打印完「上下文保留，可 resume」，紧接着 `status t` 就说
    `failed —— 报告缺失或为空＝没正常收尾`，退出码 1，两句话自相矛盾。
    前台误跑被 2 分钟超时杀掉时同理：那正是「run_in_background 编不进去」
    那条缓解措施最需要说话的时刻。

    用 O_APPEND + 单次 os.write：小写入在 Linux 上是原子的，不会和 tee 循环
    的缓冲写互相撕裂；也刻意不碰那个已经打开的文件对象——信号处理器随时可能
    插在它的 write 中间。
    写不进去就算了：保住 codex 的上下文优先于留痕。
    """
    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (INTERRUPT_MARK + "\n").encode())
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


def current_round(log_text):
    """日志是追加的，判据只看最后一个分隔符之后——上一轮的错误不是这一轮的事。"""
    text = strip_ansi(log_text)
    cut = text.rfind(ROUND_MARK)
    return text if cut < 0 else text[cut:]


def runtime_error_lines(log_text):
    """本轮日志里 codex 自己的错误行（已滤掉良性 target 与良性用户层消息）。

    刻意没有「只看末 N 行」的窗口参数。那个窗口过去偷偷承担着「运行中已经恢复
    过去的错误不算」这个语义，而这件事现在由 target 白名单正经做了，窗口只剩下
    劣化替代品的身份：留着它，下一个撞上 60 行尾部堆栈的人就会把 50 改成 500，
    然后每一次已恢复的错误都静默变成 suspect。
    """
    hits = []
    for raw in current_round(log_text).splitlines():
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


class Verdict(NamedTuple):
    state: str   # running / success / suspect / failed
    reason: str  # 一行人话
    detail: list # suspect／failed：出事的那几行；success：报告前几行


def judge(report_path, log_path, pid):
    """唯一的成败判据。`run` 收尾和 `status` 共用它，避免两处判据漂移。

    codex 的退出码不可信：中途已恢复的工具 ERROR（apply_patch 被拒后重打成功）
    也会把退出码染成 1。所以判据只看产物和日志，不看退出码。
    """
    if pid is not None:
        return Verdict("running", f"pid={pid} 存活", [])

    log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
    round_text = current_round(log_text)
    errors = runtime_error_lines(log_text)
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
        # 排在上面两条之后：那两条意味着 resume 也救不回来（换账号／新起任务），
        # 而这一条恰恰是「resume 就行」，不能把更坏的消息盖掉。
        if INTERRUPT_MARK in round_text:
            return Verdict("failed", "本轮被 INT 打断，上下文保留——接着 resume 即可，不用重跑", errors)
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


SKILL_GUARD = "**不得使用任何 skill，除非本 brief 明确指定。**"

# 每次运行都固定带上的参数。调用方碰不到它们，也就不可能漏掉。
# 主线和 resume 都收的参数。调用方碰不到它们，也就不可能漏掉。
# `--color never` **不在这里**：resume 不认它（见 build_resume_argv）。
_COMMON = ["-c", "approval_policy=\"never\"", "-c", "project_doc_max_bytes=0",
           "--skip-git-repo-check", "--disable", "plugins"]


def prepend_skill_guard(brief_text):
    """兜底句前置。CODEX_HOME 隔离是结构性防线，这句是内容层的第二道。"""
    if brief_text.startswith(SKILL_GUARD):
        return brief_text
    return f"{SKILL_GUARD}\n\n{brief_text}"


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
    needle = report_path.encode()
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


# codex 全集是 minimal/low/medium/high/xhigh/max/ultra，这五档是**刻意裁剪**。
# argparse 的 choices 是唯一守门员——实测 codex 对 `-c model_reasoning_effort=bogus`
# 静默接受、banner 照打 `reasoning effort: bogus_effort_value`，档位写错没人告诉你。
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# 四个状态，按「最该放行 → 最该拦住调用方」排序，退出码和严重度都从这**一份**
# 派生——两份清单必然漂移。
# 严重度不能直接拿退出码比：suspect 的码(3)比 failed(1)大，按码取 max 会让一个
# 真失败被一个 suspect 盖过去。
# 退出码取值一律 EXIT[state]，不写 .get(state, 默认值)：有默认值的话 running 会
# 悄悄落成 0，`codex-agent status t && deploy` 就会在任务还在跑的时候部署。
# 2 不在表里，留给参数错误与护栏拒绝（见 USAGE_ERROR）。
_STATES = (("success", 0), ("running", 4), ("suspect", 3), ("failed", 1))
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
    """唯一的 spawn 入口。开跑前必须做的三件事全在这里，调用方不需要记住顺序：
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
        log.write((round_separator(kind, task, _now_iso()) + "\n").encode())
        log.flush()

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
            # 无论包装器被谁、用什么信号停，codex 收到的永远是 INT，上下文永远可 resume。
            # 刻意不在这里退出：让 tee 循环自然跑完，判据照样出、完成通知照样带结论。
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            # 顺手留痕，让之后跑判据的人（包括另一个进程里的 status）知道
            # 这轮是被打断的，处置是 resume 而不是重跑。
            note_interrupt(_log_path(home, task))

        # 转发只在 codex 活着的这段时间里生效，出去时原样还回去——改全局信号处置
        # 而不还原，等于把本函数的副作用留给了整个进程的余生。
        previous = {sig: signal.signal(sig, forward_as_sigint)
                    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        try:
            _tee_until_exit(proc, log, home, task, meta)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


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
        if find_codex_pid(str(_report_path(home, args.task))) is not None:
            reject(f"任务名 {args.task} 还在跑，换个名字或先 `codex-agent stop {args.task}`")
        print(f"[codex-agent] 提示：任务名 {args.task} 复用，上一轮的报告会被删掉、日志会被追加")

    ensure_isolation(args.account)
    report = _report_path(home, args.task)
    brief = prepend_skill_guard(brief_file.read_text())
    print(f"[codex-agent] 已在 brief 前自动加上：{SKILL_GUARD}")

    run_codex("run", home, args.task,
              new_meta(args.task, args.account, str(workdir), args.effort),
              lambda r: build_run_argv(str(workdir), args.effort, r, brief))

    verdict = judge(report, _log_path(home, args.task), None)
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
        verdict = judge(report, log, find_codex_pid(str(report)))
        print(f"{meta['task']:<24} {meta['account']:<8} {verdict.state:<8} "
              f"{verdict.reason}  {meta['dir']}")
        for line in verdict.detail:
            print(f"    {line}")
        worst = _worse(worst, verdict.state)
    return EXIT[worst]


def cmd_resume(args):
    home, meta = find_meta(args.task)
    if meta is None:
        reject(f"没有这个任务：{args.task}")
    report = _report_path(home, args.task)
    # resume 之前必须确认真的退出了：对还在跑的会话 resume，报的错和 SIGTERM 锁死
    # 一模一样（thread-store conflict），而处置完全相反——一个该等，一个该弃。
    if find_codex_pid(str(report)) is not None:
        reject(f"任务 {args.task} 还在跑，resume 会撞上它自己的写锁。等它结束，或先 stop。")
    if not meta["session_id"]:
        reject(f"任务 {args.task} 没有记到 session id，无法 resume，只能新起一个任务")
    workdir = pathlib.Path(meta["dir"])
    if not workdir.is_dir():
        reject(f"任务 {args.task} 的工作目录 {workdir} 不在了（worktree 被删？）。"
               f"codex 会以 os error 2 当场崩，所以这里直接拒。")
    brief_file = pathlib.Path(args.brief).expanduser()
    if not brief_file.is_file():
        reject(f"--brief {args.brief} 不是文件")

    ensure_isolation(meta["account"])
    brief = prepend_skill_guard(brief_file.read_text())
    # 元数据描述的是**最后一次调用**：effort 和开跑时间都刷新。
    # 完整的轮次历史不在这里，在日志的分隔符里（每轮一行，带时间戳）。
    meta["effort"] = args.effort
    meta["started_at"] = _now_iso()
    run_codex("resume", home, args.task, meta,
              lambda r: build_resume_argv(meta["dir"], meta["session_id"], args.effort, r, brief))
    verdict = judge(report, _log_path(home, args.task), None)
    _print_verdict(args.task, verdict)
    return EXIT[verdict.state]


def cmd_stop(args):
    home, meta = find_meta(args.task)
    if meta is None:
        reject(f"没有这个任务：{args.task}")
    pid = find_codex_pid(str(_report_path(home, args.task)))
    if pid is None:
        print(f"任务 {args.task} 已经不在跑了")
        return EXIT["success"]
    # 只发 SIGINT。SIGTERM 会让 thread 永久锁死，之后 resume 永远报
    # thread-store conflict，等多久都不释放，上下文全丢。
    os.kill(pid, signal.SIGINT)
    print(f"已向 {args.task} (pid={pid}) 发 SIGINT，上下文保留，可 resume")
    return EXIT["success"]


def build_parser():
    p = argparse.ArgumentParser(
        prog="codex-agent",
        description="把执行类任务派给 codex 后台跑。用 Bash(run_in_background: true) 启动 run。")
    sub = p.add_subparsers(dest="cmd", required=True)

    # 五个参数全必填：不设默认值，因为隐式选中的账号／难度是最容易被误用的地方
    r = sub.add_parser("run", help="起一个新任务")
    r.add_argument("--task", required=True, type=task_name,
                   help="任务名，全局唯一（PID 反查和产物命名都靠它）")
    r.add_argument("--dir", required=True, help="codex 的工作目录，自动转绝对路径")
    r.add_argument("--brief", required=True, help="brief 文件路径（只收文件，不收内联字符串）")
    r.add_argument("--effort", required=True, choices=EFFORTS, help="难度分档")
    r.add_argument("--account", required=True, choices=account_choices(), help="codex 账号")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="看任务状态；省略任务名则列出全部")
    s.add_argument("task", nargs="?", type=task_name)
    s.set_defaults(func=cmd_status)

    # resume/stop 不收 --account：账号从元数据查出来，不可能指错
    m = sub.add_parser("resume", help="给已结束的任务补一轮")
    m.add_argument("task", type=task_name)
    m.add_argument("--brief", required=True)
    m.add_argument("--effort", required=True, choices=EFFORTS)
    m.set_defaults(func=cmd_resume)

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
    仍可 resume，日志里还留下一行打断标记（见 note_interrupt）告诉下一个人该
    resume 而不是重跑。于是误用的代价从「会话永久锁死、上下文全丢」降到
    「这一轮没拿到完成通知」——可恢复，且判据会把话说清楚。
    """
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except Rejected as e:
        print(e.message, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
