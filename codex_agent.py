#!/usr/bin/env python3
"""codex-agent —— 把 `codex exec` 的实测约束编译成硬约束的包装器。

调用方只给任务信息（干什么／在哪干／多难），命令组装、隔离、存活判定、
成败判据、续跑、停止全部由本文件保证。约束写在代码里而不是文档里，
是因为文档只能靠调用方记住，而记不住的代价在 SKILL.md 的历史里写满了。
"""
import re

TAIL_LINES = 50  # 判据只看日志末尾这么多行：中途已恢复的错误不该算失败

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
