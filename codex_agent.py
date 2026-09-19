#!/usr/bin/env python3
"""codex-agent —— 把 `codex exec` 的实测约束编译成硬约束的包装器。

调用方只给任务信息（干什么／在哪干／多难），命令组装、隔离、存活判定、
成败判据、续跑、停止全部由本文件保证。约束写在代码里而不是文档里，
是因为文档只能靠调用方记住，而记不住的代价在 SKILL.md 的历史里写满了。
"""
import json
import re
from typing import NamedTuple

TAIL_LINES = 50  # 判据只看日志末尾这么多行：中途已恢复的错误不该算失败
USAGE_LIMIT_MARK = "You've hit your usage limit"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# codex 自己的运行时日志有两种形态，漏掉任一种都等于判据失效（2026-09-19 实测）：
#   A 用户层    ：`ERROR: You've hit your usage limit. …`        行首就是 ERROR:
#   B tracing  ：`2026-09-18T16:49:02.380969Z ERROR codex_x::y: …` 行首是时间戳
# 而日志里还混着 brief 原文和 codex 转述的子进程输出（cargo 的 error[E0599]、
# pytest 的 `E   KeyError`、markdown 的 `## Warning Signs`），
# 所以绝不能用裸 grep ERROR —— 会大面积误报。
_RUNTIME_ERROR_PATTERNS = (
    re.compile(r"^(ERROR|WARN):\s"),
    re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+(ERROR|WARN)\s+codex\S*:"),
)

# 已知良性：出现了也不算失败
_BENIGN = (
    "failed to refresh available models",  # 模型列表刷新超时，不影响本次运行
    "Reconnecting...",                     # 网络抖动，codex 自己会重连
)

_SESSION_ID = re.compile(r"session id:\s*([0-9a-f-]{36})")


def strip_ansi(text):
    return _ANSI.sub("", text)


def runtime_error_lines(log_text, tail_lines):
    """返回日志末尾 tail_lines 行里的 codex 运行时错误行（已剥 ANSI、已滤良性）。"""
    lines = strip_ansi(log_text).splitlines()[-tail_lines:]
    hits = []
    for line in lines:
        if not any(p.search(line) for p in _RUNTIME_ERROR_PATTERNS):
            continue
        if any(b in line for b in _BENIGN):
            continue
        hits.append(line.strip())
    return hits


def extract_session_id(log_text):
    m = _SESSION_ID.search(strip_ansi(log_text))
    return m.group(1) if m else None


class Verdict(NamedTuple):
    state: str   # running / success / suspect / failed
    reason: str  # 一行人话
    detail: list # suspect：出事的那几行；success：报告顶层 key


def judge(report_path, log_path, pid):
    """唯一的成败判据。`run` 收尾和 `status` 共用它，避免两处判据漂移。

    codex 的退出码不可信：中途已恢复的工具 ERROR（apply_patch 被拒后重打成功）
    也会把退出码染成 1。所以判据只看产物和日志，不看退出码。
    """
    if pid is not None:
        return Verdict("running", f"pid={pid} 存活", [])

    log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
    errors = runtime_error_lines(log_text, TAIL_LINES)

    # “报告没出现＝没正常收尾”——这是 codex 写 -o 的唯一时机。
    # 前提是每轮开跑前把上一轮的报告删掉（见 clear_report），否则旧报告会被
    # 当成本轮的产物，一次失败的运行会被判成 success。
    if not report_path.exists() or not report_path.read_text().strip():
        if USAGE_LIMIT_MARK in strip_ansi(log_text):
            return Verdict("failed", "撞上账号额度上限，换账号或等额度恢复", errors)
        return Verdict("failed", "报告缺失或为空＝没正常收尾", errors)

    if errors:
        return Verdict("suspect", f"报告在，但日志末 {TAIL_LINES} 行有 {len(errors)} 条运行时错误", errors)

    # 报告内容由 brief 决定（要 commit 还是要别的），属于任务层不属于工具层。
    # 工具只把顶层 key 列出来，让调用方自己核对 brief 要的字段在不在。
    try:
        keys = sorted(json.loads(report_path.read_text()).keys())
    except (json.JSONDecodeError, AttributeError):
        keys = []
    return Verdict("success", "正常收尾，日志无运行时错误", keys)
