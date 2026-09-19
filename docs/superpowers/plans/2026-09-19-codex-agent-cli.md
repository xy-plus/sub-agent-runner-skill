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
  - `runtime_error_lines(log_text: str, tail_lines: int) -> list[str]`
  - `extract_session_id(log_text: str) -> str | None`
  - 常量 `TAIL_LINES = 50`

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
ERR_TRACING_UNKNOWN = ("\x1b[2m2026-09-18T16:50:00.000000Z\x1b[0m \x1b[31mERROR\x1b[0m "
                       "\x1b[2mcodex_core::rollout\x1b[0m\x1b[2m:\x1b[0m failed to persist rollout")
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
    def test_用户层ERROR行被识别(self):
        got = ca.runtime_error_lines(ERR_USER_LAYER, ca.TAIL_LINES)
        self.assertEqual(len(got), 1)
        self.assertIn("usage limit", got[0])

    def test_tracing结构化ERROR行被识别_行首是时间戳不是ERROR(self):
        # 形态 B：只按行首匹配会整类漏掉，这是 2026-09-19 差点写错的判据
        got = ca.runtime_error_lines(ERR_TRACING_UNKNOWN, ca.TAIL_LINES)
        self.assertEqual(len(got), 1)
        self.assertIn("failed to persist rollout", got[0])

    def test_已知良性行被过滤(self):
        self.assertEqual(ca.runtime_error_lines(ERR_RECONNECT, ca.TAIL_LINES), [])
        self.assertEqual(ca.runtime_error_lines(ERR_TRACING, ca.TAIL_LINES), [])

    def test_子进程输出和brief原文不算运行时ERROR(self):
        noise = "\n".join([
            "error[E0599]: no method named `cols_at` found for struct `Arc<Broker>`",
            "E   KeyError: ('2026-07-28', '1min/feature/ewm_std_hl120')",
            "**Test errors?** Fix error, re-run until it fails correctly.",
            "## Warning Signs",
            "error: test failed, to rerun pass `--lib`",
        ])
        self.assertEqual(ca.runtime_error_lines(noise, ca.TAIL_LINES), [])

    def test_只看末尾N行(self):
        log = ERR_USER_LAYER + "\n" + "\n".join(f"正常输出 {i}" for i in range(60))
        self.assertEqual(ca.runtime_error_lines(log, ca.TAIL_LINES), [])

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
- Consumes: `runtime_error_lines`、`TAIL_LINES`
- Produces:
  - `Verdict = NamedTuple("Verdict", [("state", str), ("reason", str), ("detail", list)])`
  - `judge(report_path: pathlib.Path, log_path: pathlib.Path, pid) -> Verdict`
  - 常量 `USAGE_LIMIT_MARK = "You've hit your usage limit"`
  - 状态字符串 `"running" / "success" / "suspect" / "failed"`

- [ ] **Step 1: 写失败的测试**

```python
import pathlib, tempfile

class TestJudge(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.report = self.d / "t.json"
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

    def test_报告在且日志干净是success_并列出顶层key(self):
        self.report.write_text('{"commit": "abc", "summary": "done"}')
        v = ca.judge(self.report, self.log, None)
        self.assertEqual(v.state, "success")
        self.assertEqual(sorted(v.detail), ["commit", "summary"])

    def test_报告不是JSON也算success_那是任务层的事(self):
        # 报告内容由 brief 决定，工具层只管"有没有正常收尾"
        self.report.write_text("干完了，见分支 feat/x")
        v = ca.judge(self.report, self.log, None)
        self.assertEqual(v.state, "success")
        self.assertEqual(v.detail, [])

    def test_报告在但日志尾有未知运行时ERROR是suspect(self):
        self.report.write_text('{"ok": 1}')
        self.log.write_text("2026-09-18T16:50:00.000000Z ERROR codex_core::rollout: failed to persist rollout")
        v = ca.judge(self.report, self.log, None)
        self.assertEqual(v.state, "suspect")
        self.assertEqual(len(v.detail), 1)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent -v`
Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'judge'`

- [ ] **Step 3: 最小实现**

```python
import json
from typing import NamedTuple

USAGE_LIMIT_MARK = "You've hit your usage limit"


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

    # "报告没出现＝没正常收尾"——这是 codex 写 -o 的唯一时机
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
  - 常量 `CONFIG_BASELINE: str`

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
        self.assertTrue((d / "skills").is_dir())
        self.assertTrue((d / "plugins").is_dir())
        self.assertTrue((d / "config.toml").is_file())
        self.assertTrue((d / "auth.json").is_symlink())
        self.assertIn("danger-full-access", (d / "config.toml").read_text())

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
    """保证隔离目录满足全部不变量，不满足就拒跑（而不是"尽力而为"地继续）。"""
    d = isolation_home(account)
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
        argv = ca.build_run_argv("/abs/repo", "low", "/d/reports/t.json", "brief")
        self.assertEqual(argv[:3], ["codex", "exec", "--cd"])
        self.assertEqual(argv[3], "/abs/repo")
        self.assertIn("--sandbox", argv)
        self.assertIn("danger-full-access", argv)
        self.assertIn('model_reasoning_effort="low"', " ".join(argv))
        self.assertEqual(argv[-1], "brief")
        self.assertEqual(argv[argv.index("-o") + 1], "/d/reports/t.json")

    def test_resume的cd在resume之前_否则clap直接拒收(self):
        argv = ca.build_resume_argv("/abs/repo", "sess-1", "low", "/d/reports/t.json", "再来一轮")
        self.assertLess(argv.index("--cd"), argv.index("resume"))
        self.assertEqual(argv[argv.index("resume") + 1], "sess-1")

    def test_resume不许出现sandbox长选项_它不认(self):
        argv = ca.build_resume_argv("/abs/repo", "sess-1", "low", "/d/reports/t.json", "x")
        self.assertNotIn("--sandbox", argv)
        self.assertIn('sandbox_mode="danger-full-access"', " ".join(argv))

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
_COMMON = ["-c", "approval_policy=\"never\"", "-c", "project_doc_max_bytes=0",
           "--skip-git-repo-check", "--disable", "plugins"]


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

    $! 拿到的是包装链最外层（2026-09-17 实测：$! 是 254151，codex 是 254153），
    据此判"已退出"再 resume，会撞上它自己的写锁。
    报告路径在 codex 的 argv 里且按任务唯一，所以反查从它入手；
    再按 comm 收窄——pgrep -f 会命中发命令的 shell 自己（2026-09-19 实测）。
    """
    r = subprocess.run(["pgrep", "-f", report_path], capture_output=True, text=True)
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
  - `run_codex(argv: list, env: dict, log_path, home, task) -> None`（tee 到屏幕与日志，边跑边抓 session id）
  - `main() -> int`
  - 常量 `EXIT = {"success": 0, "failed": 1, "suspect": 3}`

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

    def test_没有session_id就拒绝(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t2", {"task": "t2", "account": "default", "dir": "/tmp", "effort": "low"})
        args = ca.build_parser().parse_args(["resume", "t2", "--brief", str(self.brief), "--effort", "low"])
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            with self.assertRaises(SystemExit) as cm:
                ca.cmd_resume(args)
        self.assertIn("session id", str(cm.exception))

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

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
EXIT = {"success": 0, "failed": 1, "suspect": 3}  # 不用 2：argparse 的参数错误占了


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


def run_codex(argv, env, log_path, home, task):
    """起 codex，输出同时进屏幕和日志，边跑边把 session id 记进元数据。

    stdin 固定接 /dev/null：否则 codex 等 stdin 永久挂死（日志只剩
    "Reading additional input from stdin" + 进程 0% CPU）。
    不设 timeout：会误杀正当的长任务。
    """
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    head, session_id = b"", None
    with open(log_path, "wb") as log:
        for chunk in iter(lambda: proc.stdout.read(1024), b""):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            log.write(chunk)
            log.flush()
            if session_id is None:
                head += chunk
                session_id = extract_session_id(head.decode("utf-8", "replace"))
                if session_id:
                    meta = json.loads(meta_path(home, task).read_text())
                    meta["session_id"] = session_id
                    meta["pid"] = proc.pid
                    write_meta(home, task, meta)
    proc.wait()


def _report_path(home, task):
    return home / "reports" / f"{task}.json"


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
        if find_codex_pid(str(_report_path(old_home, args.task))) is not None:
            raise SystemExit(f"任务名 {args.task} 还在跑，换个名字或先 `codex-agent stop {args.task}`")
        print(f"[codex-agent] 提示：任务名 {args.task} 复用，上一轮的报告和日志会被覆盖")

    home = ensure_isolation(args.account)
    report = _report_path(home, args.task)
    log = home / "logs" / f"{args.task}.log"
    brief = prepend_skill_guard(brief_file.read_text())
    print(f"[codex-agent] 已在 brief 前自动加上：{SKILL_GUARD}")

    write_meta(home, args.task, {
        "task": args.task, "account": args.account, "dir": str(workdir),
        "effort": args.effort, "session_id": None, "pid": None,
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    run_codex(build_run_argv(str(workdir), args.effort, str(report), brief),
              codex_env(home), log, home, args.task)

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
        worst = max(worst, EXIT.get(verdict.state, 0))
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
    run_codex(build_resume_argv(meta["dir"], meta["session_id"], args.effort, str(report), brief),
              codex_env(home), log, home, args.task)
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
   - 退出码：`0` success、`1` failed、`3` suspect（suspect＝干完了但日志尾有运行时错误，要人看一眼）。
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
