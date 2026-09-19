# codex-agent CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `codex exec` 的 12 条软约束编译进一个可执行文件，让调用方只提供任务信息（干什么／在哪干／多难），其余全部由工具保证。

**Architecture:** 单文件 Python CLI（`codex_agent.py`），四个子命令 `run/status/resume/stop`。纯函数（ANSI 剥离、ERROR 识别、session id 提取、判据、argv 组装）与副作用（进程、文件系统）分离，纯函数全部单测覆盖。启动仍由调用方用 `Bash(run_in_background: true)` 执行，保留 harness 的完成通知。

**Tech Stack:** Python 3（仅 stdlib：`argparse`／`subprocess`／`unittest`／`re`／`json`／`pathlib`）。

## Global Constraints

- 对照 spec：`docs/superpowers/specs/2026-09-19-codex-agent-cli-design.md`，每个决定以 spec 为准。
- **零第三方依赖**，只用 Python3 stdlib。
- **不设任何默认缺省值**：`run` 的五个参数全必填；内部函数也不写默认参数值，由调用方显式传常量。
- 模型固定 `MODEL = "gpt-6-astra"`，难度只由 `--effort` 分档。
- 退出码：`0` success、`1` failed、`3` suspect（**不用 2**，argparse 的参数错误占用了 2）。
  实测确认后台任务的完成通知里带退出码（exit 3 的通知写的就是 failed with exit code 3），
  所以「run 的退出码＝判据结论」这个核心收益成立。
- 报告文件是 `reports/<任务>.md`：`-o` 写的是 agent 的最后一条**消息**，实测 156 份里
  只有 4 份是 JSON，其余都是 markdown 散文（P1-1）。
- 文件：`codex_agent.py`、`test_codex_agent.py`，均在仓库根目录。测试命令 `python3 -m unittest test_codex_agent -v`。
- 每个任务结束提交一次，提交信息中文、说清楚为什么。
- **注释承载知识**：spec 与旧 SKILL.md 里的实测教训（日期＋当时怎么炸的）写进代码注释，贴在防住它的那行旁边，不另开文档。

---

### Task 1: log 解析纯函数（ANSI／运行时 ERROR／session id）

**Files:**
- Create: `codex_agent.py`
- Test: `test_codex_agent.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `strip_ansi(text: str) -> str`
  - `round_separator(kind: str, task: str, when_iso: str) -> str`
  - `current_round(log_text: str) -> str`
  - `runtime_error_lines(log_text: str) -> list[str]`
  - `extract_session_id(log_text: str) -> str | None`
  - 常量 `ROUND_MARK`、`_BENIGN_TARGETS`、`_BENIGN_USER`

**审查后修订（P0-2 / P0-3 / P1-2）：**
- 错误有**三种**锚定形式，不是两种：新增形式 C 顶层致命 `Error:`（大写 E），
  而它正是 spec §9 整节在讲的那个 thread-store conflict。
- 形式 B 按 **module target** 分类，不按自由文本（文本会变，target 不会）。
- **删掉 `TAIL_LINES`**：日志改成追加 + 每轮写分隔符，判据只扫最后一个分隔符之后。
  「末 50 行」那个窗口在偷偷承担「已恢复的错误不算」的语义，而这件事现在由
  target 分类正经做了，窗口成了劣化替代品。
- `--color never` 之后日志本就无 ANSI，`strip_ansi` 降级为防御、不再承重；
  测试仍喂带 ANSI 的输入，因为要防的就是它万一还在。

- [ ] **Step 1: 写失败的测试**

```python
import unittest
import codex_agent as ca

# 真实日志片段（2026-09-19 从 ~/.claude/jobs/2e6058df/tmp/codex-*.log 取）
ERR_USER_LAYER = "\x1b[1m\x1b[31mERROR:\x1b[0m\x1b[0m You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage"
ERR_RECONNECT = "\x1b[1m\x1b[31mERROR:\x1b[0m\x1b[0m Reconnecting... 2/5"
ERR_TRACING = ("\x1b[2m2026-09-18T16:49:02.380969Z\x1b[0m \x1b[31mERROR\x1b[0m "
               "\x1b[2mcodex_models_manager::manager\x1b[0m\x1b[2m:\x1b[0m "
               "failed to refresh available models: timeout waiting for child process to exit")
ERR_TRACING_UNKNOWN = ("\x1b[2m2026-09-17T14:35:32.578919Z\x1b[0m \x1b[31mERROR\x1b[0m "
                       "\x1b[2mcodex_core::session\x1b[0m\x1b[2m:\x1b[0m Failed to create session: "
                       "thread-store conflict: thread already has an active writer")
ERR_FATAL = "Error: thread/resume: thread 01a0… already has an active writer (code -32600)"
ERR_TRACING_MODELS = ERR_TRACING          # codex_models_manager::*，良性
ERR_TRACING_ROUTER = ("2026-09-18T16:49:02.380969Z ERROR codex_core::tools::router: "
                      "apply_patch failed: file changed on disk")
ERR_TRACING_WS = ("2026-09-18T16:49:02.380969Z ERROR codex_api::endpoint::responses_websocket: "
                  "websocket closed unexpectedly")
ERR_SESSION = ERR_TRACING_UNKNOWN
HEADER = ("Reading additional input from stdin...\n"
          "OpenAI Codex v0.154.0\n"
          "--------\n"
          "\x1b[1mworkdir:\x1b[0m /home/xy/repo\n"
          "\x1b[1msession id:\x1b[0m 01a0b408-f718-7ff3-8123-d5202551acba\n"
          "--------\n")

class TestStripAnsi(unittest.TestCase):
    def test_去掉颜色码只留文字(self):
        self.assertEqual(ca.strip_ansi(ERR_RECONNECT), "ERROR: Reconnecting... 2/5")

class TestRuntimeErrorLines(unittest.TestCase):
    def test_形式A用户层ERROR行被识别(self):
        got = ca.runtime_error_lines(ERR_USER_LAYER)
        self.assertEqual(len(got), 1)
        self.assertIn("usage limit", got[0])

    def test_形式B按target分类_未知target计入(self):
        # 行首是时间戳不是 ERROR：只按行首匹配会把整类结构化日志漏掉
        got = ca.runtime_error_lines(ERR_TRACING_UNKNOWN)
        self.assertEqual(len(got), 1)
        self.assertIn("thread-store conflict", got[0])

    def test_形式C顶层致命Error大写E也要认_它正是写锁那条(self):
        got = ca.runtime_error_lines(ERR_FATAL)
        self.assertEqual(len(got), 1)
        self.assertIn("already has an active writer", got[0])

    def test_良性target被过滤(self):
        for benign in (ERR_TRACING_MODELS, ERR_TRACING_ROUTER, ERR_TRACING_WS):
            self.assertEqual(ca.runtime_error_lines(benign), [])

    def test_Reconnecting按前缀过滤_后缀有多种写整行会漏(self):
        for suffix in ("2/5", "5/5", "waiting for network"):
            self.assertEqual(ca.runtime_error_lines(f"ERROR: Reconnecting... {suffix}"), [])

    def test_codex_core_session不是良性_它是最该报的那条(self):
        self.assertEqual(len(ca.runtime_error_lines(ERR_SESSION)), 1)

    def test_子进程输出和brief原文不算运行时ERROR(self):
        noise = "\n".join([
            "error[E0599]: no method named `cols_at` found for struct `Arc<Broker>`",
            "E   KeyError: ('2026-07-28', '1min/feature/ewm_std_hl120')",
            "**Test errors?** Fix error, re-run until it fails correctly.",
            "## Warning Signs",
            "error: test failed, to rerun pass `--lib`",
        ])
        self.assertEqual(ca.runtime_error_lines(noise), [])

class TestCurrentRound(unittest.TestCase):
    def test_只扫最后一个分隔符之后_上一轮的错误不算这一轮的(self):
        log = (ca.round_separator("run", "t", "2026-09-19T10:00:00") + "\n" + ERR_FATAL + "\n"
               + ca.round_separator("resume", "t", "2026-09-19T11:00:00") + "\n干净收尾\n")
        self.assertEqual(ca.runtime_error_lines(log), [])

    def test_没有分隔符时扫全文_老日志和半路接手都还能判(self):
        self.assertEqual(len(ca.runtime_error_lines(ERR_FATAL)), 1)

class TestExtractSessionId(unittest.TestCase):
    def test_从带ANSI的日志头提取(self):
        self.assertEqual(ca.extract_session_id(HEADER),
                         "01a0b408-f718-7ff3-8123-d5202551acba")

    def test_还没打出来时返回None(self):
        self.assertIsNone(ca.extract_session_id("OpenAI Codex v0.154.0\n"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'codex_agent'`

- [ ] **Step 3: 最小实现**

```python
#!/usr/bin/env python3
"""codex-agent —— 把 `codex exec` 的实测约束编译成硬约束的包装器。

调用方只给任务信息（干什么／在哪干／多难），命令组装、隔离、存活判定、
成败判据、续跑、停止全部由本文件保证。约束写在代码里而不是文档里，
是因为文档只能靠调用方记住，而记不住的代价在 SKILL.md 的历史里写满了。
"""
import re

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

ROUND_MARK = "===== codex-agent "   # 每轮开跑前写进日志的分隔符前缀


def round_separator(kind, task, when_iso):
    return f"{ROUND_MARK}{kind} {task} {when_iso} ====="


# codex 自己的错误有三种锚定形式（2026-09-19 对 106 份真实日志全量统计），
# 少认一种就等于判据失效：
#   A 用户层    ：`ERROR: Reconnecting... 2/5`                     行首是 ERROR:／WARN:
#   B tracing  ：`<ISO 时间戳> ERROR codex_core::session: …`        行首是时间戳，带 target
#   C 顶层致命 ：`Error: thread/resume: … active writer`           行首是大写 Error:
# 形式 C 的首字母是大写，`^ERROR:` 大小写敏感，匹配不到它——而它正是「会话被锁死」
# 那条最该报的错。
# 日志里还混着 brief 原文和 codex 转述的子进程输出（cargo 的 error[E0599]、
# pytest 的 `E   KeyError`、markdown 的 `## Warning Signs`），
# 所以绝不能用裸 grep ERROR —— 会大面积误报。
_ERR_USER = re.compile(r"^(?:ERROR|WARN):\s+(.*)")
_ERR_TRACING = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+(?:ERROR|WARN)\s+(\S+?):\s")
_ERR_FATAL = re.compile(r"^Error:\s")          # 形式 C，一律致命，无白名单

# 形式 B 按 module target 分类，不按自由文本——文本会变，target 不会。
_BENIGN_TARGETS = (
    "codex_models_manager::",                      # 模型列表刷新超时，不影响本次运行
    "codex_api::endpoint::responses_websocket",    # 连接抖动，自己会重连
    "rmcp::transport::worker",
    "codex_core::tools::router",                   # apply_patch 被拒后重打成功
)
# 形式 A 的良性只有这一条，且必须按**前缀**匹配：后缀有 `2/5`~`5/5` 和
# `waiting for network` 多种，写整行字面量会漏。
_BENIGN_USER = ("Reconnecting...",)

_SESSION_ID = re.compile(r"session id:\s*([0-9a-f-]{36})")


def strip_ansi(text):
    """防御性剥离。有 `--color never` 之后日志本就是纯文本，这里不再承重。"""
    return _ANSI.sub("", text)


def current_round(log_text):
    """日志是追加的，判据只看最后一个分隔符之后——上一轮的错误不是这一轮的事。"""
    text = strip_ansi(log_text)
    cut = text.rfind(ROUND_MARK)
    return text if cut < 0 else text[cut:]


def runtime_error_lines(log_text):
    """本轮日志里 codex 自己的错误行（已滤掉良性 target 和良性用户层消息）。

    刻意没有「只看末 N 行」的窗口参数：那个窗口过去偷偷承担着「已恢复的错误
    不算」的语义，而这件事现在由 target 白名单正经做了。留着窗口，下一个撞上
    60 行尾部堆栈的人就会把 50 改成 500，然后每次已恢复的错误都静默变 suspect。
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，8 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: log 解析纯函数——两种形态的运行时 ERROR 判据与 session id 提取"
```

---

### Task 2: 判据函数 `judge()`

**Files:**
- Modify: `codex_agent.py`
- Test: `test_codex_agent.py`

**Interfaces:**
- Consumes: `runtime_error_lines`、`current_round`
- Produces:
  - `Verdict = NamedTuple("Verdict", [("state", str), ("reason", str), ("detail", list)])`
  - `judge(report_path: pathlib.Path, log_path: pathlib.Path, pid) -> Verdict`
  - `clear_report(report_path) -> None`
  - 常量 `USAGE_LIMIT_MARK`、`THREAD_LOCK_MARK`、`REPORT_PREVIEW_LINES = 5`
  - 状态字符串 `"running" / "success" / "suspect" / "failed"`

**审查后修订（P0-1 / P1-1）：**
- **`clear_report`**：codex **只在正常收尾时**写 `-o` 文件、启动时不 truncate。
  run 成功写下报告 → resume 秒死于写锁 → 判据读到**上一轮的旧报告** → 报 success。
  真实日志里有 5 份样本走的正是这条路。run 和 resume 都必须在 spawn 之前删掉它，
  本工具成为报告的唯一创建者，「报告存在」才重新是一句关于本次调用的真话。
- success 的 `detail` 从「JSON 顶层 key」改成「报告前 5 行」：实测 156 份报告
  只有 4 份能解析成 JSON，`-o` 写的是 agent 的最后一条消息，通常是 markdown 散文。
- `thread-store conflict` 和 `usage limit` 一样特判进 `reason`，不新增状态。

- [ ] **Step 1: 写失败的测试**

```python
import pathlib, tempfile

class TestJudge(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.report = self.d / "t.md"
        self.log = self.d / "t.log"
        self.log.write_text("正常收尾\n")

    def test_PID还活着就是running_不看产物(self):
        v = ca.judge(self.report, self.log, 12345)
        self.assertEqual(v.state, "running")

    def test_报告缺失是failed(self):
        v = ca.judge(self.report, self.log, None)
        self.assertEqual(v.state, "failed")
        self.assertIn("没正常收尾", v.reason)

    def test_报告为空也是failed(self):
        self.report.write_text("")
        v = ca.judge(self.report, self.log, None)
        self.assertEqual(v.state, "failed")

    def test_报告缺失且撞额度上限_reason要点名(self):
        self.log.write_text("\x1b[1m\x1b[31mERROR:\x1b[0m\x1b[0m You've hit your usage limit. Visit https://x")
        v = ca.judge(self.report, self.log, None)
        self.assertEqual(v.state, "failed")
        self.assertIn("额度", v.reason)

    def test_报告是散文也算success_并预览前几行(self):
        # 实测 156 份报告只有 4 份是 JSON，-o 写的是 agent 的最后一条消息
        self.report.write_text("干完了，见分支 feat/x\n改了 3 个文件\n测试全绿\n")
        v = ca.judge(self.report, self.log, None)
        self.assertEqual(v.state, "success")
        self.assertEqual(v.detail[0], "干完了，见分支 feat/x")

    def test_预览最多几行_长报告不刷屏(self):
        self.report.write_text("\n".join(f"第 {i} 行" for i in range(50)))
        v = ca.judge(self.report, self.log, None)
        self.assertEqual(len(v.detail), ca.REPORT_PREVIEW_LINES)

    def test_报告在但本轮日志有未分类错误是suspect(self):
        self.report.write_text("干完了")
        self.log.write_text("2026-09-17T14:35:32.578919Z ERROR codex_core::session: "
                            "Failed to create session: thread-store conflict")
        v = ca.judge(self.report, self.log, None)
        self.assertEqual(v.state, "suspect")
        self.assertEqual(len(v.detail), 1)

    def test_撞上写锁_reason要点名(self):
        self.log.write_text("Error: thread/resume: 01a0… already has an active writer (code -32600)")
        v = ca.judge(self.report, self.log, None)
        self.assertEqual(v.state, "failed")
        self.assertIn("锁", v.reason)

    def test_清报告之后旧内容不会被当成本轮产物(self):
        self.report.write_text("上一轮的报告")
        ca.clear_report(self.report)
        v = ca.judge(self.report, self.log, None)
        self.assertEqual(v.state, "failed")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent -v`
Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'judge'`

- [ ] **Step 3: 最小实现**

```python
import json
from typing import NamedTuple

USAGE_LIMIT_MARK = "You've hit your usage limit"
THREAD_LOCK_MARK = "already has an active writer"
REPORT_PREVIEW_LINES = 5


def clear_report(report_path):
    """每轮开跑前删掉报告。codex 只在正常收尾时写 -o、启动时不 truncate，
    留着上一轮的报告，一次秒死于写锁的 resume 就会被判成 success——工具在说谎
    （2026-09-19 实测，真实日志里 5 份样本走的正是这条路）。
    删了之后本工具是报告的唯一创建者，「报告存在」才是关于本次调用的真话。
    代价是失败的 resume 会连带毁掉上一轮的报告：可接受，日志是追加的，还在。
    """
    report_path.unlink(missing_ok=True)


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
    round_text = current_round(log_text)
    errors = runtime_error_lines(log_text)

    # 「报告没出现＝没正常收尾」——这是 codex 写 -o 的唯一时机。
    # 成立的前提是每轮开跑前 clear_report 过，否则读到的是上一轮的旧报告。
    if not report_path.exists() or not report_path.read_text().strip():
        # 两种特判只改 reason、不新增状态：补救手段不同，状态机不该为此变复杂。
        if USAGE_LIMIT_MARK in round_text:
            return Verdict("failed", "撞上账号额度上限，换账号或等额度恢复", errors)
        if THREAD_LOCK_MARK in round_text:
            return Verdict("failed", "会话被写锁占住（上一轮没真的结束，或曾被 SIGTERM 杀过），只能新起一个任务", errors)
        return Verdict("failed", "报告缺失或为空＝没正常收尾", errors)

    if errors:
        return Verdict("suspect", f"报告在，但本轮日志有 {len(errors)} 条未分类的 codex 错误", errors)

    # 报告内容由 brief 决定（要 commit 还是要别的），属于任务层不属于工具层。
    # 工具只预览前几行，让调用方自己核对 brief 要的东西在不在。
    preview = [l for l in report_path.read_text().splitlines() if l.strip()][:REPORT_PREVIEW_LINES]
    return Verdict("success", "正常收尾，本轮日志无未分类错误", preview)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，15 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: 判据函数 judge——四态收敛，退出码不参与判断"
```

---

### Task 3: 隔离目录不变量

**Files:**
- Modify: `codex_agent.py`
- Test: `test_codex_agent.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `account_choices() -> list[str]`
  - `isolation_home(account: str) -> pathlib.Path`
  - `auth_source(account: str) -> pathlib.Path`
  - `ensure_isolation(account: str) -> pathlib.Path`（违反不变量时 `raise SystemExit(str)`）
  - `shared_skill_root() -> pathlib.Path`
  - 常量 `CONFIG_NOTE: str`

**审查后修订（P0-4 / P2-2）：**
- **`config.toml` 的内容不是不变量**：实测 codex 自己往里写
  `[projects."…"] trust_level = "trusted"`，`~/.codex-subagent` 已累积 19 段。
  校验内容则第二次 run 就失败，重写则抹掉 codex 的 trust 状态。
  缺失时只创建一份**仅含说明注释**的空配置，唯一的不变量是「它是普通文件，不是软链」。
- `model` / `model_reasoning_effort` / `sandbox_mode` / `approval_policy`
  **一律不写进 config**：CLI 每次都显式传，config 再存一份就是同一条事实两个家，
  还是个会被静默覆盖的缺省值。
- 新增不变量：`~/.agents/skills/` 必须为空。那是 `CODEX_HOME` **管不到**的共享扫描根，
  放了东西 codex 就看得见，隔离的前提直接不成立——**非空即拒跑**，并列出里面有什么。

- [ ] **Step 1: 写失败的测试**

```python
import os
from unittest import mock

class TestIsolation(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.p = mock.patch.object(ca.pathlib.Path, "home", staticmethod(lambda: self.home))
        self.p.start()
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").write_text("{}")
        (self.home / ".codex-accounts" / "acct2").mkdir(parents=True)
        (self.home / ".codex-accounts" / "acct2" / "auth.json").write_text("{}")

    def tearDown(self):
        self.p.stop()

    def test_账号可选项来自实际目录扫描(self):
        self.assertEqual(ca.account_choices(), ["default", "acct2"])

    def test_default账号映射到不带后缀的隔离目录(self):
        self.assertEqual(ca.isolation_home("default"), self.home / ".codex-subagent")
        self.assertEqual(ca.isolation_home("acct2"), self.home / ".codex-subagent-acct2")

    def test_首次使用自动建齐目录与配置(self):
        d = ca.ensure_isolation("acct2")
        for sub in ("skills", "plugins", "tasks", "reports", "logs"):
            self.assertTrue((d / sub).is_dir())
        self.assertTrue((d / "config.toml").is_file())
        self.assertTrue((d / "auth.json").is_symlink())

    def test_生成的config不写模型与沙箱_那些由CLI每次显式传(self):
        d = ca.ensure_isolation("acct2")
        text = (d / "config.toml").read_text()
        for key in ("model", "model_reasoning_effort", "sandbox_mode", "approval_policy"):
            self.assertNotIn(f"{key} =", text)

    def test_共享扫描根非空就拒跑_CODEX_HOME管不到它(self):
        (self.home / ".agents" / "skills" / "某个skill").mkdir(parents=True)
        with self.assertRaises(SystemExit) as cm:
            ca.ensure_isolation("default")
        self.assertIn("某个skill", str(cm.exception))

    def test_config是软链就拒跑_隔离会失效(self):
        d = self.home / ".codex-subagent"
        d.mkdir()
        (d / "config.toml").symlink_to(self.home / ".codex" / "config.toml")
        with self.assertRaises(SystemExit) as cm:
            ca.ensure_isolation("default")
        self.assertIn("软链", str(cm.exception))

    def test_已有的config不被覆盖(self):
        d = self.home / ".codex-subagent"
        d.mkdir()
        (d / "config.toml").write_text('model = "自定义"\n')
        ca.ensure_isolation("default")
        self.assertIn("自定义", (d / "config.toml").read_text())

    def test_账号没登录态就拒跑(self):
        (self.home / ".codex-accounts" / "acct3").mkdir()
        with self.assertRaises(SystemExit) as cm:
            ca.ensure_isolation("acct3")
        self.assertIn("登录", str(cm.exception))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent -v`
Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'account_choices'`

- [ ] **Step 3: 最小实现**

```python
import pathlib

MODEL = "gpt-6-astra"

# 隔离目录自己的 config，绝不软链主配置。
# 2026 年踩过：`codex-acct` 把 config.toml 软链到主配置，一用就把 MCP、plugins、
# hooks、memories 全带回来，隔离当场失效。账号和隔离是正交的两件事，要组合。
#
# 这份初始内容**刻意只有注释**：model / effort / sandbox_mode / approval_policy
# 由 CLI 每次显式传，config 里再存一份就是同一条事实两个家，还是个会被静默
# 覆盖的缺省值。而且 codex 自己会往这个文件里追加 [projects.*] trust_level，
# 所以内容不是不变量——不校验、不重写，只保证它是普通文件（见 ensure_isolation）。
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
    """保证隔离目录满足全部不变量，不满足就拒跑（而不是"尽力而为"地继续）。"""
    d = isolation_home(account)
    # tasks/reports/logs 必须先建好：目录不存在时 codex 不会自己建，`-o` 静默
    # 写失败（log 末尾只留一行 Failed to write last message file），而判据是
    # "报告没出现＝没正常收尾"——一次成功的运行会被判成失败。2026-09-13 连踩两次。
    for sub in ("skills", "plugins", "tasks", "reports", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)

    # 拒跑而不是警告：隔离的前提被破坏时，本工具的核心承诺是空的，
    # 而警告会被淹没在几千行日志里没人看见。
    intruders = sorted(p.name for p in shared_skill_root().iterdir()) if shared_skill_root().is_dir() else []
    if intruders:
        raise SystemExit(
            f"{shared_skill_root()} 非空：{', '.join(intruders)}\n"
            f"那是 CODEX_HOME 管不到的共享扫描根，放了东西 codex 就看得见，隔离不成立。清空它再跑。")

    config = d / "config.toml"
    # 唯一的 config 不变量：普通文件。内容不校验也不重写——codex 自己会往里写
    # [projects.*] trust_level（~/.codex-subagent 已累积 19 段），校验内容则第二次
    # run 就失败，重写则抹掉 codex 的 trust 状态。
    if config.is_symlink():
        raise SystemExit(
            f"{config} 是软链——隔离会失效（软链主配置会把 MCP/plugins/hooks 全带回来）。\n"
            f"请删掉它，重跑本命令会生成一份新的。")
    if not config.exists():
        config.write_text(CONFIG_NOTE)

    src = auth_source(account)
    if not src.exists():
        raise SystemExit(f"账号 {account} 没有登录态（{src} 不存在）。先跑 `codex-acct login {account}`。")
    auth = d / "auth.json"
    if not (auth.is_symlink() and auth.resolve() == src.resolve()):
        if auth.exists() or auth.is_symlink():
            auth.unlink()
        auth.symlink_to(src)
    return d
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，21 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: 隔离目录不变量——config.toml 软链即拒跑"
```

---

### Task 4: 命令行组装与 brief 兜底句

**Files:**
- Modify: `codex_agent.py`
- Test: `test_codex_agent.py`

**Interfaces:**
- Consumes: `MODEL`
- Produces:
  - `prepend_skill_guard(brief_text: str) -> str`
  - `build_run_argv(dir_abs: str, effort: str, report_path: str, brief: str) -> list[str]`
  - `build_resume_argv(dir_abs: str, session_id: str, effort: str, report_path: str, brief: str) -> list[str]`
  - `codex_env(home: pathlib.Path) -> dict`
  - 常量 `SKILL_GUARD: str`

- [ ] **Step 1: 写失败的测试**

```python
class TestArgv(unittest.TestCase):
    def test_兜底句被前置且只加一次(self):
        once = ca.prepend_skill_guard("干活")
        self.assertTrue(once.startswith(ca.SKILL_GUARD))
        self.assertEqual(ca.prepend_skill_guard(once), once)

    def test_run参数完整(self):
        argv = ca.build_run_argv("/abs/repo", "low", "/d/reports/t.md", "brief")
        self.assertEqual(argv[:3], ["codex", "exec", "--cd"])
        self.assertEqual(argv[3], "/abs/repo")
        self.assertIn("--sandbox", argv)
        self.assertIn("danger-full-access", argv)
        self.assertIn('model_reasoning_effort="low"', " ".join(argv))
        self.assertEqual(argv[-1], "brief")
        self.assertEqual(argv[argv.index("-o") + 1], "/d/reports/t.md")

    def test_resume的cd在resume之前_否则clap直接拒收(self):
        argv = ca.build_resume_argv("/abs/repo", "sess-1", "low", "/d/reports/t.md", "再来一轮")
        self.assertLess(argv.index("--cd"), argv.index("resume"))
        self.assertEqual(argv[argv.index("resume") + 1], "sess-1")

    def test_resume不许出现sandbox长选项_它不认(self):
        argv = ca.build_resume_argv("/abs/repo", "sess-1", "low", "/d/reports/t.md", "x")
        self.assertNotIn("--sandbox", argv)
        self.assertIn('sandbox_mode="danger-full-access"', " ".join(argv))

    def test_两条命令都从源头关掉颜色(self):
        for argv in (ca.build_run_argv("/abs/repo", "low", "/d/reports/t.md", "b"),
                     ca.build_resume_argv("/abs/repo", "s", "low", "/d/reports/t.md", "b")):
            self.assertEqual(argv[argv.index("--color") + 1], "never")

    def test_环境变量把会话索引留在主目录_resume才找得到(self):
        env = ca.codex_env(pathlib.Path("/d"))
        self.assertEqual(env["CODEX_HOME"], "/d")
        self.assertEqual(env["CODEX_SQLITE_HOME"], str(pathlib.Path.home() / ".codex"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent -v`
Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'prepend_skill_guard'`

- [ ] **Step 3: 最小实现**

```python
import os

SKILL_GUARD = "**不得使用任何 skill，除非本 brief 明确指定。**"

# 每次运行都固定带上的参数。调用方碰不到它们，也就不可能漏掉。
# --color never：实测 --color auto（默认）在输出被重定向时并不关颜色，106 份日志
# 无一例外含 ANSI，于是提 session id 和跑判据要各自剥一遍。从源头关掉，两个消费方
# 都不再依赖剥离器（strip_ansi 保留作防御，但不再承重）。
_COMMON = ["-c", "approval_policy=\"never\"", "-c", "project_doc_max_bytes=0",
           "--skip-git-repo-check", "--disable", "plugins", "--color", "never"]


def prepend_skill_guard(brief_text):
    """兜底句前置。CODEX_HOME 隔离是结构性防线，这句是内容层的第二道。"""
    if brief_text.startswith(SKILL_GUARD):
        return brief_text
    return f"{SKILL_GUARD}\n\n{brief_text}"


def build_run_argv(dir_abs, effort, report_path, brief):
    # --cd 必须绝对路径：相对路径启动即崩（log 无 banner + os error 2）
    return (["codex", "exec", "--cd", dir_abs, "-m", MODEL,
             "-c", f'model_reasoning_effort="{effort}"',
             "--sandbox", "danger-full-access"] + _COMMON +
            ["-o", report_path, brief])


def build_resume_argv(dir_abs, session_id, effort, report_path, brief):
    # 两处和主线不同，都是实测撞出来的：
    #   1. --cd 必须放在 resume 之前，放后面 clap 直接拒收
    #   2. resume 不认 --sandbox（error: unexpected argument，退出码 2），走 -c sandbox_mode
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，26 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: argv 组装——主线与 resume 的参数差异编码进代码"
```

---

### Task 5: 任务元数据与真实 PID 反查

**Files:**
- Modify: `codex_agent.py`
- Test: `test_codex_agent.py`

**Interfaces:**
- Consumes: `isolation_home`、`account_choices`
- Produces:
  - `meta_path(home: pathlib.Path, task: str) -> pathlib.Path`
  - `write_meta(home, task, meta: dict) -> None`
  - `find_meta(task: str) -> dict | None`（跨所有隔离目录查，含 `_home` 键指回目录）
  - `all_metas() -> list[dict]`
  - `find_codex_pid(report_path: str) -> int | None`
  - `pid_alive(pid: int) -> bool`

- [ ] **Step 1: 写失败的测试**

```python
class TestMeta(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.p = mock.patch.object(ca.pathlib.Path, "home", staticmethod(lambda: self.home))
        self.p.start()
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").write_text("{}")

    def tearDown(self):
        self.p.stop()

    def test_元数据写入后能跨隔离目录查回来(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t1", {"task": "t1", "account": "default", "dir": "/abs/x", "effort": "low"})
        got = ca.find_meta("t1")
        self.assertEqual(got["dir"], "/abs/x")
        self.assertEqual(got["_home"], str(d))

    def test_查不到返回None(self):
        self.assertIsNone(ca.find_meta("不存在的任务"))

    def test_同名任务出现在两个隔离目录就拒绝_不许猜(self):
        (self.home / ".codex-accounts" / "acct2").mkdir(parents=True)
        (self.home / ".codex-accounts" / "acct2" / "auth.json").write_text("{}")
        for account in ("default", "acct2"):
            ca.write_meta(ca.ensure_isolation(account), "撞名", {"task": "撞名"})
        with self.assertRaises(SystemExit) as cm:
            ca.find_meta("撞名")
        self.assertIn("多个隔离目录", str(cm.exception))

    def test_列出全部任务(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "a", {"task": "a"})
        ca.write_meta(d, "b", {"task": "b"})
        self.assertEqual(sorted(m["task"] for m in ca.all_metas()), ["a", "b"])

class TestPid(unittest.TestCase):
    def test_自己的进程判定为存活(self):
        self.assertTrue(ca.pid_alive(os.getpid()))

    def test_不存在的进程判定为已退出(self):
        self.assertFalse(ca.pid_alive(2 ** 22))

    def test_没权限发信号意味着进程存在_不是已退出(self):
        # EPERM 是「有这个进程但不归你管」，只有 ESRCH 才是已退出。
        # 把 EPERM 当死，就会误判「已结束」而去 resume 一个还在跑的会话。
        with mock.patch.object(ca.os, "kill", side_effect=PermissionError):
            self.assertTrue(ca.pid_alive(1))

    def test_只认comm是codex的进程_shell自己不算(self):
        # 2026-09-19 实测：pgrep -f <报告路径> 会命中发命令的 bash 自己（comm=bash），
        # comm 过滤是承重的，不是保险。这里用一个 argv 含该路径的 sleep 验证它被排除。
        mark = "/tmp/codex-agent-selftest-不存在的报告.json"
        proc = __import__("subprocess").Popen(["sleep", "5", mark])
        try:
            self.assertIsNone(ca.find_codex_pid(mark))
        finally:
            proc.kill(); proc.wait()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent -v`
Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'meta_path'`

- [ ] **Step 3: 最小实现**

```python
import subprocess


def meta_path(home, task):
    return home / "tasks" / f"{task}.json"


def write_meta(home, task, meta):
    meta_path(home, task).write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def find_meta(task):
    """跨所有隔离目录按任务名找。账号是查出来的，不是让调用方再报一遍的。

    查到多份就拒绝，不"取第一个"：那会让 status/resume/stop 静默作用到
    扫描顺序更靠前的那个会话上，而任务名撞车这条路很好走（撞额度上限 →
    换账号重跑同名任务）。cmd_run 已经不让这个状态建起来，这里是第二道。
    """
    found = []
    for account in account_choices():
        p = meta_path(isolation_home(account), task)
        if p.exists():
            meta = json.loads(p.read_text())
            meta["_home"] = str(isolation_home(account))
            found.append(meta)
    if len(found) > 1:
        raise SystemExit(
            f"任务名 {task} 在多个隔离目录里都有："
            + "、".join(m["_home"] for m in found)
            + "\n无法确定该操作哪一个，删掉不要的那份元数据再来。")
    return found[0] if found else None


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

    $! 拿到的是包装链最外层（2026-09-17 实测：$! 是 254151，codex 是 254153），
    据此判"已退出"再 resume，会撞上它自己的写锁。
    报告路径在 codex 的 argv 里且按任务唯一，所以反查从它入手；
    再按 comm 收窄——pgrep -f 会命中发命令的 shell 自己（2026-09-19 实测）。
    """
    r = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", report_path],
                       capture_output=True, text=True)
    for pid_str in r.stdout.split():
        comm = subprocess.run(["ps", "-o", "comm=", "-p", pid_str],
                              capture_output=True, text=True).stdout.strip()
        if comm == "codex" and pid_alive(int(pid_str)):
            return int(pid_str)
    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，32 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: 任务元数据与真实 PID 反查——comm 过滤是承重的"
```

---

### Task 6: CLI 层与四个子命令

**Files:**
- Modify: `codex_agent.py`
- Test: `test_codex_agent.py`

**Interfaces:**
- Consumes: 前五个 Task 的全部产出
- Produces:
  - `build_parser() -> argparse.ArgumentParser`
  - `cmd_run(args) -> int`、`cmd_status(args) -> int`、`cmd_resume(args) -> int`、`cmd_stop(args) -> int`
  - `run_codex(argv: list, env: dict, log_path, home, task, kind) -> None`
    （写本轮分隔符、tee 到屏幕与日志、边跑边抓 session id、把信号统一转成 INT）
  - `main() -> int`
  - 常量 `EXIT = {"success": 0, "failed": 1, "suspect": 3, "running": 4}`

**审查后修订：**
- **P0-1**：`cmd_run` / `cmd_resume` 都在 spawn 之前 `clear_report(report)`。
- **P0-3**：日志改**追加**（`"ab"`），`run_codex` 进来先写一行本轮分隔符。
  `run_codex` 因此多收一个 `kind`（`run`／`resume`）——分隔符由它自己写，
  调用方不可能忘。
- **P1-1**：报告路径 `reports/<任务>.md`；PID 反查的字符串跟着变。
- **P2-1**：元数据**不存 `pid`**。存活必须每次重新反查，存一个会过期、还会被
  系统复用的 PID，只会诱导别人犯这个设计本来要防的错。
- **P2-3**：`EFFORTS` 旁注明五档是对 codex 全集
  （`minimal/low/medium/high/xhigh/max/ultra`）的刻意裁剪，且 argparse 的
  `choices` 是唯一守门员——实测 codex 对 `-c model_reasoning_effort=bogus`
  **静默接受**、banner 照打 `reasoning effort: bogus_effort_value`。

**实现时发现、计划原稿没有的三处（工作子代理补）：**
- `proc.stdout.read(1024)` 会**阻塞到攒够 1024 字节**，tee 就不是实时的，
  session id 也要等攒够才写进元数据（`status` 在这段窗口里查不到它）。
  改用 `read1(1024)`：有多少给多少。
- `EXIT.get(state, 0)` 是**默认缺省值**，而 `running` 恰好落进这个默认里——
  `codex-agent status t && 下一步` 会把「还在跑」当成功。改成四个状态都在表里、
  用 `EXIT[state]` 直接取，缺哪个就 KeyError 当场炸，不静默给 0。
- **同一个任务名出现在两个隔离目录**里时，`find_meta` 只返回先扫到的那个，
  `status`／`resume`／`stop` 会静默作用到错的会话上。而这条路很好走：撞额度上限
  → 换账号重跑同名任务。所以 `find_meta` 查到多份就**拒绝并列出**，
  `cmd_run` 发现任务名已属于别的账号也**拒绝**——让这个状态压根建不起来。

- [ ] **Step 1: 写失败的测试**

```python
class TestParser(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.p = mock.patch.object(ca.pathlib.Path, "home", staticmethod(lambda: self.home))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_run的五个参数一个都不能少(self):
        parser = ca.build_parser()
        for missing in ["--task", "--dir", "--brief", "--effort", "--account"]:
            argv = ["run", "--task", "t", "--dir", "/tmp", "--brief", "b.md",
                    "--effort", "low", "--account", "default"]
            i = argv.index(missing)
            del argv[i:i + 2]
            with self.subTest(missing=missing), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_effort只收五个档位(self):
        parser = ca.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["run", "--task", "t", "--dir", "/tmp", "--brief", "b.md",
                               "--effort", "中等", "--account", "default"])

    def test_resume和stop不收account_账号是查出来的(self):
        parser = ca.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["resume", "t", "--brief", "b.md", "--effort", "low",
                               "--account", "default"])

    def test_不提供会造成误用的参数(self):
        parser = ca.build_parser()
        for bad in ["--timeout", "-o", "--model", "--background"]:
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parser.parse_args(["run", "--task", "t", "--dir", "/tmp", "--brief", "b.md",
                                   "--effort", "low", "--account", "default", bad, "x"])

class TestRunGuards(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.p = mock.patch.object(ca.pathlib.Path, "home", staticmethod(lambda: self.home))
        self.p.start()
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").write_text("{}")
        self.workdir = self.home / "repo"
        self.workdir.mkdir()
        self.brief = self.home / "brief.md"
        self.brief.write_text("干活")

    def tearDown(self):
        self.p.stop()

    def _args(self, **over):
        argv = ["run", "--task", over.get("task", "t"), "--dir", over.get("dir", str(self.workdir)),
                "--brief", over.get("brief", str(self.brief)), "--effort", "low", "--account", "default"]
        return ca.build_parser().parse_args(argv)

    def test_dir不是目录就拒跑(self):
        with self.assertRaises(SystemExit) as cm:
            ca.cmd_run(self._args(dir=str(self.home / "没有这个目录")))
        self.assertIn("不是目录", str(cm.exception))

    def test_brief不是文件就拒跑(self):
        with self.assertRaises(SystemExit) as cm:
            ca.cmd_run(self._args(brief=str(self.home / "没有这个文件.md")))
        self.assertIn("brief", str(cm.exception))

    def test_同名任务还在跑就拒绝(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", {"task": "t", "account": "default", "dir": str(self.workdir)})
        with mock.patch.object(ca, "find_codex_pid", return_value=99999):
            with self.assertRaises(SystemExit) as cm:
                ca.cmd_run(self._args(task="t"))
        self.assertIn("还在跑", str(cm.exception))

    def test_同名任务属于别的账号就拒绝_否则之后指向哪个都不确定(self):
        (self.home / ".codex-accounts" / "acct2").mkdir(parents=True)
        (self.home / ".codex-accounts" / "acct2" / "auth.json").write_text("{}")
        ca.write_meta(ca.ensure_isolation("acct2"), "t", {"task": "t", "account": "acct2"})
        with self.assertRaises(SystemExit) as cm:
            ca.cmd_run(self._args(task="t"))
        self.assertIn("acct2", str(cm.exception))

    def test_开跑前删掉上一轮的报告_否则旧报告会被判成本轮成功(self):
        d = ca.ensure_isolation("default")
        (d / "reports" / "t.md").write_text("上一轮的报告")
        with mock.patch.object(ca, "run_codex") as fake:
            ca.cmd_run(self._args(task="t"))
        self.assertFalse((d / "reports" / "t.md").exists())
        self.assertTrue(fake.called)

    def test_日志是追加的_上一轮的内容不会被冲掉(self):
        d = ca.ensure_isolation("default")
        (d / "logs" / "t.log").write_text("上一轮的日志\n")
        with mock.patch.object(ca, "run_codex"):
            ca.cmd_run(self._args(task="t"))
        self.assertIn("上一轮的日志", (d / "logs" / "t.log").read_text())

class TestResumeGuards(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.p = mock.patch.object(ca.pathlib.Path, "home", staticmethod(lambda: self.home))
        self.p.start()
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").write_text("{}")
        self.brief = self.home / "b.md"
        self.brief.write_text("再来")

    def tearDown(self):
        self.p.stop()

    def test_任务不存在就报错(self):
        args = ca.build_parser().parse_args(["resume", "没有", "--brief", str(self.brief), "--effort", "low"])
        with self.assertRaises(SystemExit) as cm:
            ca.cmd_resume(args)
        self.assertIn("没有这个任务", str(cm.exception))

    def test_还在跑就拒绝resume_写锁冲突和SIGTERM锁死长得一样(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", {"task": "t", "account": "default", "dir": "/tmp",
                               "session_id": "s1", "effort": "low"})
        args = ca.build_parser().parse_args(["resume", "t", "--brief", str(self.brief), "--effort", "low"])
        with mock.patch.object(ca, "find_codex_pid", return_value=99999):
            with self.assertRaises(SystemExit) as cm:
                ca.cmd_resume(args)
        self.assertIn("还在跑", str(cm.exception))

    def test_resume开跑前也要删报告_秒死于写锁时才不会误判成功(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t3", {"task": "t3", "account": "default", "dir": "/tmp",
                                "session_id": "s1", "effort": "low"})
        (d / "reports" / "t3.md").write_text("上一轮的报告")
        args = ca.build_parser().parse_args(["resume", "t3", "--brief", str(self.brief), "--effort", "low"])
        with mock.patch.object(ca, "find_codex_pid", return_value=None), \
             mock.patch.object(ca, "run_codex"):
            ca.cmd_resume(args)
        self.assertFalse((d / "reports" / "t3.md").exists())

    def test_没有session_id就拒绝(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t2", {"task": "t2", "account": "default", "dir": "/tmp", "effort": "low"})
        args = ca.build_parser().parse_args(["resume", "t2", "--brief", str(self.brief), "--effort", "low"])
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            with self.assertRaises(SystemExit) as cm:
                ca.cmd_resume(args)
        self.assertIn("session id", str(cm.exception))

class TestSignalSafety(unittest.TestCase):
    """codex 只能死于 INT——这是整个工具最不能出错的一条保证。"""

    def _run_once(self, popen):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "tasks").mkdir()
        popen.return_value.stdout.read1.return_value = b""
        popen.return_value.wait.return_value = 0
        ca.run_codex(["codex"], {}, d / "t.log", d, "t", "run")

    def test_codex起在独立会话里_组信号打不到它(self):
        with mock.patch.object(ca.subprocess, "Popen") as popen:
            self._run_once(popen)
        self.assertIs(popen.call_args.kwargs["start_new_session"], True)

    def test_包装器收到SIGTERM时向codex转发的是SIGINT(self):
        with mock.patch.object(ca.subprocess, "Popen") as popen, \
             mock.patch.object(ca.signal, "signal") as sigsig:
            self._run_once(popen)
            handled = {c.args[0] for c in sigsig.call_args_list}
            self.assertEqual(handled, {signal.SIGTERM, signal.SIGINT, signal.SIGHUP})
            sigsig.call_args_list[0].args[1](signal.SIGTERM, None)
        popen.return_value.send_signal.assert_called_with(signal.SIGINT)

class TestStop(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.p = mock.patch.object(ca.pathlib.Path, "home", staticmethod(lambda: self.home))
        self.p.start()
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").write_text("{}")

    def tearDown(self):
        self.p.stop()

    def test_只发SIGINT_绝不发SIGTERM(self):
        import signal
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", {"task": "t", "account": "default", "dir": "/tmp"})
        args = ca.build_parser().parse_args(["stop", "t"])
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca.os, "kill") as k:
            ca.cmd_stop(args)
        k.assert_called_once_with(4242, signal.SIGINT)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent -v`
Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'build_parser'`

- [ ] **Step 3: 最小实现**

```python
import argparse
import datetime
import signal
import sys

# codex 全集是 minimal/low/medium/high/xhigh/max/ultra，这五档是**刻意裁剪**：
# minimal 弱到不值得派活，ultra 贵到该由人自己决定要不要。
# argparse 的 choices 是唯一守门员——实测 codex 对 `-c model_reasoning_effort=bogus`
# 静默接受、banner 照打 `reasoning effort: bogus_effort_value`，错档位不会有人告诉你。
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# 四个状态都在表里，取值用 EXIT[state] 不用 .get(state, 0)：
# 有默认值的话 running 会悄悄落成 0，`codex-agent status t && 下一步` 就把
# 「还在跑」当成了成功。2 不用——argparse 的参数错误占了。
EXIT = {"success": 0, "failed": 1, "suspect": 3, "running": 4}


def build_parser():
    p = argparse.ArgumentParser(
        prog="codex-agent",
        description="把执行类任务派给 codex 后台跑。用 Bash(run_in_background: true) 启动 run。")
    sub = p.add_subparsers(dest="cmd", required=True)

    # 五个参数全必填：不设默认值，因为隐式选中的账号／难度是最容易被误用的地方
    r = sub.add_parser("run", help="起一个新任务")
    r.add_argument("--task", required=True, help="任务名，全局唯一（PID 反查和产物命名都靠它）")
    r.add_argument("--dir", required=True, help="codex 的工作目录，自动转绝对路径")
    r.add_argument("--brief", required=True, help="brief 文件路径（只收文件，不收内联字符串）")
    r.add_argument("--effort", required=True, choices=EFFORTS, help="难度分档")
    r.add_argument("--account", required=True, choices=account_choices(), help="codex 账号")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="看任务状态；省略任务名则列出全部")
    s.add_argument("task", nargs="?")
    s.set_defaults(func=cmd_status)

    # resume/stop 不收 --account：账号从元数据查出来，不可能指错
    m = sub.add_parser("resume", help="给已结束的任务补一轮")
    m.add_argument("task")
    m.add_argument("--brief", required=True)
    m.add_argument("--effort", required=True, choices=EFFORTS)
    m.set_defaults(func=cmd_resume)

    k = sub.add_parser("stop", help="停一个任务（只发 SIGINT）")
    k.add_argument("task")
    k.set_defaults(func=cmd_stop)
    return p


def run_codex(argv, env, log_path, home, task, kind):
    """起 codex，输出同时进屏幕和日志，边跑边把 session id 记进元数据。

    stdin 固定接 /dev/null：否则 codex 等 stdin 永久挂死（日志只剩
    "Reading additional input from stdin" + 进程 0% CPU）。
    不设 timeout：会误杀正当的长任务。

    start_new_session=True 不是为了 detach，是为了挡组信号。2026-09-19 实测：
    codex 与包装器同进程组时，一发 `kill -TERM -<组>`（harness 停后台任务就是这么干的）
    会直接把 codex TERM 死，而 SIGTERM 之后 thread 永久锁死、再也 resume 不了。
    隔到独立会话后，codex 收不到任何组信号，只会收到下面 handler 转发的 INT。
    """
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)

    def forward_as_sigint(signum, frame):
        # 无论包装器被谁、用什么信号停，codex 收到的永远是 INT，上下文永远可 resume。
        # 不在这里退出：让 tee 循环跑完，判据照样出、通知照样带结论。
        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, forward_as_sigint)

    head, session_id = b"", None
    # 日志追加不覆盖，进来先写一行本轮分隔符——判据只扫它之后的内容。
    # 分隔符由 run_codex 自己写，调用方不可能忘，忘了判据就会把上一轮的错误
    # 算到这一轮头上。
    with open(log_path, "ab") as log:
        log.write((round_separator(kind, task, _now_iso()) + "\n").encode())
        log.flush()
        # read1：有多少读多少。read(1024) 会阻塞到攒够 1024 字节，tee 就不实时，
        # session id 也要等攒够才落盘，这段窗口里 status 查不到它。
        for chunk in iter(lambda: proc.stdout.read1(1024), b""):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            log.write(chunk)
            log.flush()
            if session_id is None and len(head) < _HEAD_LIMIT:
                head += chunk
                session_id = extract_session_id(head.decode("utf-8", "replace"))
                if session_id:
                    # 刻意不存 pid：存活每次重新反查，存下来的 PID 会过期、会被复用。
                    meta = json.loads(meta_path(home, task).read_text())
                    meta["session_id"] = session_id
                    write_meta(home, task, meta)
    proc.wait()


# session id 在 banner 里，前几百字节就出现。攒到这个上限还没有就不再攒，
# 免得几 MB 的输出全留在内存里。
_HEAD_LIMIT = 8192


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _report_path(home, task):
    # .md 不是 .json：-o 写的是 agent 的最后一条消息，实测 156 份里只有 4 份
    # 能解析成 JSON，其余都是 markdown 散文。后缀名要说真话。
    return home / "reports" / f"{task}.md"


def _print_verdict(task, verdict):
    print(f"\n[codex-agent] {task}: {verdict.state} —— {verdict.reason}")
    for line in verdict.detail:
        print(f"  {line}")


def cmd_run(args):
    workdir = pathlib.Path(args.dir).expanduser().resolve()
    if not workdir.is_dir():
        raise SystemExit(f"--dir {args.dir} 不是目录")
    brief_file = pathlib.Path(args.brief).expanduser()
    if not brief_file.is_file():
        raise SystemExit(f"--brief {args.brief} 不是文件（brief 只收文件路径，避开引号地狱）")

    existing = find_meta(args.task)
    if existing is not None:
        old_home = pathlib.Path(existing["_home"])
        # 换账号重跑同名任务很好走（撞额度上限时就该这么干），但那会让同一个名字
        # 出现在两个隔离目录里，之后 status/resume/stop 只能靠扫描顺序二选一，
        # 静默作用到错的会话上。所以这个状态压根不让它建起来。
        if old_home != isolation_home(args.account):
            raise SystemExit(
                f"任务名 {args.task} 已经属于账号 {existing.get('account')}（{old_home}）。\n"
                f"同名任务跨账号会让 status/resume/stop 指向哪个变得不确定，换个任务名。")
        if find_codex_pid(str(_report_path(old_home, args.task))) is not None:
            raise SystemExit(f"任务名 {args.task} 还在跑，换个名字或先 `codex-agent stop {args.task}`")
        print(f"[codex-agent] 提示：任务名 {args.task} 复用，上一轮的报告会被删掉、日志会被追加")

    home = ensure_isolation(args.account)
    report = _report_path(home, args.task)
    log = home / "logs" / f"{args.task}.log"
    brief = prepend_skill_guard(brief_file.read_text())
    print(f"[codex-agent] 已在 brief 前自动加上：{SKILL_GUARD}")

    write_meta(home, args.task, {
        "task": args.task, "account": args.account, "dir": str(workdir),
        "effort": args.effort, "session_id": None,
        "started_at": _now_iso(),
    })
    clear_report(report)   # 必须在 spawn 之前：否则上一轮的报告会被当成本轮的产物
    run_codex(build_run_argv(str(workdir), args.effort, str(report), brief),
              codex_env(home), log, home, args.task, "run")

    verdict = judge(report, log, None)
    _print_verdict(args.task, verdict)
    print(f"  报告 {report}\n  日志 {log}")
    return EXIT[verdict.state]


def cmd_status(args):
    metas = [find_meta(args.task)] if args.task else all_metas()
    if metas == [None]:
        raise SystemExit(f"没有这个任务：{args.task}")
    if not metas:
        print("还没有任何任务")
        return 0
    worst = 0
    for meta in metas:
        home = pathlib.Path(meta["_home"])
        report, log = _report_path(home, meta["task"]), home / "logs" / f"{meta['task']}.log"
        verdict = judge(report, log, find_codex_pid(str(report)))
        print(f"{meta['task']:<24} {meta.get('account', '?'):<8} {verdict.state:<8} "
              f"{verdict.reason}  {meta.get('dir', '')}")
        for line in verdict.detail:
            print(f"    {line}")
        worst = max(worst, EXIT[verdict.state])
    return worst


def cmd_resume(args):
    meta = find_meta(args.task)
    if meta is None:
        raise SystemExit(f"没有这个任务：{args.task}")
    home = pathlib.Path(meta["_home"])
    report = _report_path(home, args.task)
    # resume 之前必须确认真的退出了：对还在跑的会话 resume，报的错和 SIGTERM 锁死
    # 一模一样（thread-store conflict），而处置完全相反——一个该等，一个该弃。
    if find_codex_pid(str(report)) is not None:
        raise SystemExit(f"任务 {args.task} 还在跑，resume 会撞上它自己的写锁。等它结束，或先 stop。")
    if not meta.get("session_id"):
        raise SystemExit(f"任务 {args.task} 没有记到 session id，无法 resume，只能新起一个任务")
    brief_file = pathlib.Path(args.brief).expanduser()
    if not brief_file.is_file():
        raise SystemExit(f"--brief {args.brief} 不是文件")

    log = home / "logs" / f"{args.task}.log"
    ensure_isolation(meta["account"])
    brief = prepend_skill_guard(brief_file.read_text())
    meta["effort"] = args.effort
    write_meta(home, args.task, {k: v for k, v in meta.items() if k != "_home"})
    clear_report(report)   # 同 run：秒死于写锁的 resume 会读到上一轮的报告并报 success
    run_codex(build_resume_argv(meta["dir"], meta["session_id"], args.effort, str(report), brief),
              codex_env(home), log, home, args.task, "resume")
    verdict = judge(report, log, None)
    _print_verdict(args.task, verdict)
    return EXIT[verdict.state]


def cmd_stop(args):
    meta = find_meta(args.task)
    if meta is None:
        raise SystemExit(f"没有这个任务：{args.task}")
    home = pathlib.Path(meta["_home"])
    pid = find_codex_pid(str(_report_path(home, args.task)))
    if pid is None:
        print(f"任务 {args.task} 已经不在跑了")
        return 0
    # 只发 SIGINT。SIGTERM 会让 thread 永久锁死，之后 resume 永远报
    # thread-store conflict，等多久都不释放，上下文全丢。
    os.kill(pid, signal.SIGINT)
    print(f"已向 {args.task} (pid={pid}) 发 SIGINT，上下文保留，可 resume")
    return 0


def main():
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

注意：`build_parser` 里引用了 `cmd_run` 等函数，Python 在函数体执行时才解析名字，
所以定义顺序不影响——但为了读起来顺，把 `build_parser` 放在四个 `cmd_*` 之后。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，全部测试绿

- [ ] **Step 5: 加执行权限并自检**

```bash
chmod +x codex_agent.py
python3 codex_agent.py --help
python3 codex_agent.py run --help
```
Expected: 帮助正常打印，`--account` 的 choices 里能看到 `default acct2 acct3`

- [ ] **Step 6: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: CLI 四个子命令——参数强制显式，resume/stop 的账号从元数据查出来"
```

---

### Task 7: 改写 SKILL.md 并安装入口

**Files:**
- Modify: `SKILL.md`（250 行 → 约 40 行）
- Create: 软链 `~/.local/bin/codex-agent`

**Interfaces:**
- Consumes: `codex_agent.py` 的 CLI 契约
- Produces: 新 SKILL.md

- [ ] **Step 1: 建软链并验证命令可用**

```bash
ln -sfn "$PWD/codex_agent.py" "$HOME/.local/bin/codex-agent"
codex-agent --help
```
Expected: 帮助正常打印

- [ ] **Step 2: 改写 SKILL.md**

新 SKILL.md 只保留**人／模型才能决定的事**，全文约 40 行，包含且仅包含：

1. frontmatter：`name: codex-agent`，description 说明"派执行类任务给 codex 后台跑，省 claude token；用 Bash run_in_background 启动"。
2. 一句话定位：把**有明确规划的执行类任务**派给 codex 干，claude 只编排。
3. 启动方式：

   ````markdown
   ## 启动：一个任务 = 一次 `Bash(run_in_background: true)`

   ```bash
   codex-agent run --task <任务名> --dir /abs/repo --brief brief.md --effort low --account default
   ```

   五个参数**全必填**，没有默认值。`run_in_background: true` 是唯一正确的启动方式——
   harness 追踪它、面板可监控、**完成时的通知里直接带成败结论**。
   绝不 `nohup … &`：detach 之后就只剩存活、没有通知。
   ````
4. effort 分档表（从旧 SKILL.md 原样搬，这是判断力，不是约束）。
5. 另外三条命令各一行：

   ```bash
   codex-agent status [任务名]        # 省略则列出全部；running/success/suspect/failed
   codex-agent resume <任务名> --brief follow.md --effort low
   codex-agent stop <任务名>          # 只发 SIGINT，上下文保留
   ```
6. 三件调用方仍需要知道的事：
   - 退出码：`0` success、`1` failed、`3` suspect（干完了但本轮日志有未分类的 codex 错误，
     要人看一眼）、`4` running（`status` 才会出现）。
   - brief 只收**文件路径**；工具会自动前置"不得使用任何 skill"兜底句。
   - 要传 skill 给 codex：在 brief 里写该 skill 的**绝对路径**让它自己读，不要动共享目录。
7. 一句收尾：codex 不靠谱就换 claude 子代理。

**不写进 SKILL.md 的**（已经是代码保证的，写了就是重复）：`--cd` 绝对路径、`</dev/null`、
`-o` 路径、`mkdir -p`、不加 `&`／timeout、PID 怎么反查、`kill -INT`、
exit 1 ≠ 失败、resume 的 flag 差异、隔离目录怎么建。

- [ ] **Step 3: 核对行数与内容**

```bash
wc -l SKILL.md
grep -cE '\-\-cd|/dev/null|pgrep|kill -INT|mkdir -p' SKILL.md
```
Expected: 行数 ≤ 50；第二条命中数为 0（这些约束已在代码里，文档不该再说一遍）

- [ ] **Step 4: 提交**

```bash
git add SKILL.md
git commit -m "docs: SKILL.md 收敛到 40 行——约束进了代码，文档只留判断力"
```

---

### Task 8: 端到端冒烟

**Files:**
- 无新文件（验证性任务）

**Interfaces:**
- Consumes: 全部

- [ ] **Step 1: 造一个最小的真任务**

```bash
mkdir -p /tmp/codex-agent-smoke && cd /tmp/codex-agent-smoke && git init -q 2>/dev/null
cat > /tmp/codex-agent-smoke/brief.md <<'EOF'
在当前目录创建文件 hello.txt，内容为一行：codex-agent smoke ok
然后输出一个 JSON 收尾自述，包含字段 file（创建的文件名）和 done（true）。
EOF
```

- [ ] **Step 2: 跑起来（必须用 Bash run_in_background: true）**

```bash
codex-agent run --task smoke-2026-09-19 --dir /tmp/codex-agent-smoke \
  --brief /tmp/codex-agent-smoke/brief.md --effort low --account default
```
Expected: 屏幕有 codex 的实时输出；结束时打印 `smoke-2026-09-19: success —— 正常收尾，日志无运行时错误`，退出码 0

- [ ] **Step 3: 验证产物与判据**

```bash
cat /tmp/codex-agent-smoke/hello.txt
codex-agent status smoke-2026-09-19
cat ~/.codex-subagent/tasks/smoke-2026-09-19.json
cat ~/.codex-subagent/reports/smoke-2026-09-19.md
```
Expected: 文件内容正确；status 报 success；元数据里 `session_id` 非 null

- [ ] **Step 4: 验证 resume**

```bash
echo "再创建 hello2.txt，内容 second round，然后输出同样格式的 JSON 收尾自述。" > /tmp/codex-agent-smoke/follow.md
codex-agent resume smoke-2026-09-19 --brief /tmp/codex-agent-smoke/follow.md --effort low
cat /tmp/codex-agent-smoke/hello2.txt
```
Expected: resume 成功，第二个文件出现（证明 `--cd` 与 session 都正确恢复）

- [ ] **Step 5: 验证护栏真的拦得住**

```bash
codex-agent run --task smoke-2026-09-19 --dir /不存在 --brief /tmp/codex-agent-smoke/brief.md --effort low --account default; echo "退出码 $?"
codex-agent run --task x --dir /tmp --brief /tmp/codex-agent-smoke/brief.md --effort 中等 --account default; echo "退出码 $?"
codex-agent resume 不存在的任务 --brief /tmp/codex-agent-smoke/brief.md --effort low; echo "退出码 $?"
```
Expected: 三条全部被拒绝并给出人话错误信息

- [ ] **Step 6: 清理并提交冒烟结论**

```bash
rm -rf /tmp/codex-agent-smoke
git commit --allow-empty -m "test: 端到端冒烟通过——run/status/resume/护栏四项验证"
```
