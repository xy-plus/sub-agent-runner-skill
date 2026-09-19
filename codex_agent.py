#!/usr/bin/env python3
"""codex-agent —— 把 `codex exec` 的实测约束编译成硬约束的包装器。

调用方只给任务信息（干什么／在哪干／多难），命令组装、隔离、存活判定、
成败判据、续跑、停止全部由本文件保证。约束写在代码里而不是文档里，
是因为文档只能靠调用方记住，而记不住的代价在 SKILL.md 的历史里写满了。
"""
import json
import os
import pathlib
import re
import subprocess
from typing import NamedTuple

USAGE_LIMIT_MARK = "You've hit your usage limit"
THREAD_LOCK_MARK = "already has an active writer"
REPORT_PREVIEW_LINES = 5

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

ROUND_MARK = "===== codex-agent "   # 每轮开跑前写进日志的分隔符前缀


def round_separator(kind, task, when_iso):
    return f"{ROUND_MARK}{kind} {task} {when_iso} ====="


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
    """防御性剥离。有了 `--color never`，日志本来就是纯文本，这里不再承重。"""
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

    # “报告没出现＝没正常收尾”——这是 codex 写 -o 的唯一时机。
    # 前提是每轮开跑前把上一轮的报告删掉（见 clear_report），否则旧报告会被
    # 当成本轮的产物，一次失败的运行会被判成 success。
    if not report_path.exists() or not report_path.read_text().strip():
        # 两种特判只改 reason、不新增状态：补救手段不同（换账号／新起任务），
        # 但都属于「没正常收尾」这一种事实，状态机不该为此变复杂。
        if USAGE_LIMIT_MARK in round_text:
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
    preview = [l for l in report_path.read_text().splitlines() if l.strip()][:REPORT_PREVIEW_LINES]
    return Verdict("success", "正常收尾，本轮日志无未分类错误", preview)


MODEL = "gpt-6-astra"

# 隔离目录自己的 config，绝不软链主配置。
# 2026 年踩过：`codex-acct` 把 config.toml 软链到主配置，一用就把 MCP、plugins、
# hooks、memories 全带回来，隔离当场失效。账号和隔离是正交的两件事，要组合。
CONFIG_BASELINE = f'''model = "{MODEL}"
model_reasoning_effort = "medium"
approval_policy = "never"
sandbox_mode = "danger-full-access"
service_tier = "default"
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


def ensure_isolation(account):
    """保证隔离目录满足全部不变量，不满足就拒跑（而不是“尽力而为”地继续）。"""
    d = isolation_home(account)
    # tasks/reports/logs 必须先建好：目录不存在时 codex 不会自己建，`-o` 静默
    # 写失败（log 末尾只留一行 Failed to write last message file），而判据是
    # “报告没出现＝没正常收尾”——一次成功的运行会被判成失败。2026-09-13 连踩两次。
    for sub in ("skills", "plugins", "tasks", "reports", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)

    config = d / "config.toml"
    if config.is_symlink():
        raise SystemExit(
            f"{config} 是软链——隔离会失效（软链主配置会把 MCP/plugins/hooks 全带回来）。\n"
            f"请删掉它，重跑本命令会生成一份独立的安全基线配置。")
    if not config.exists():
        config.write_text(CONFIG_BASELINE)

    src = auth_source(account)
    if not src.exists():
        raise SystemExit(f"账号 {account} 没有登录态（{src} 不存在）。先跑 `codex-acct login {account}`。")
    auth = d / "auth.json"
    if not (auth.is_symlink() and auth.resolve() == src.resolve()):
        if auth.exists() or auth.is_symlink():
            auth.unlink()
        auth.symlink_to(src)
    return d


SKILL_GUARD = "**不得使用任何 skill，除非本 brief 明确指定。**"

# 每次运行都固定带上的参数。调用方碰不到它们，也就不可能漏掉。
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
    return (["codex", "exec", "--cd", dir_abs, "-m", MODEL,
             "-c", f'model_reasoning_effort="{effort}"',
             "--sandbox", "danger-full-access"] + _COMMON +
            ["-o", report_path, brief])


def build_resume_argv(dir_abs, session_id, effort, report_path, brief):
    # 两处和主线不同，都是实测撞出来的：
    #   1. --cd 必须放在 resume 之前，放后面 clap 直接拒收
    #   2. resume 不认 --sandbox（error: unexpected argument，退出码 2），走 -c sandbox_mode
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


def find_meta(task):
    """跨所有隔离目录按任务名找。账号是查出来的，不是让调用方再报一遍的。"""
    for account in account_choices():
        p = meta_path(isolation_home(account), task)
        if p.exists():
            meta = json.loads(p.read_text())
            meta["_home"] = str(isolation_home(account))
            return meta
    return None


def all_metas():
    out = []
    for account in account_choices():
        home = isolation_home(account)
        tasks_dir = home / "tasks"
        if not tasks_dir.is_dir():
            continue
        for p in sorted(tasks_dir.glob("*.json")):
            meta = json.loads(p.read_text())
            meta["_home"] = str(home)
            out.append(meta)
    return out


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def find_codex_pid(report_path):
    """存活判定只有一个可靠判据：真实 PID。日志判不了，$! 给不出。

    回合用尽的 codex 留下的 log 和还在跑的长得一模一样（末尾都是正常输出、
    没有收尾标记），所以 tail 日志只能看它在干什么，判不了存活。
    $! 拿到的是包装链最外层（2026-09-17 实测：$! 是 254151，codex 是 254153），
    据此判"已退出"再 resume，会撞上它自己的写锁。
    报告路径在 codex 的 argv 里且按任务唯一，所以反查从它入手；
    再按 comm 收窄——pgrep -f 会命中发命令的 shell 自己（2026-09-19 实测，
    comm=bash），少了这道过滤会把 shell 当成 codex。
    """
    r = subprocess.run(["pgrep", "-f", report_path], capture_output=True, text=True)
    for pid_str in r.stdout.split():
        comm = subprocess.run(["ps", "-o", "comm=", "-p", pid_str],
                              capture_output=True, text=True).stdout.strip()
        if comm == "codex" and pid_alive(int(pid_str)):
            return int(pid_str)
    return None
