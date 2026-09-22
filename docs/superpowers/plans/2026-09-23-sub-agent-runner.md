# sub-agent-runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 同一个工具既能派 codex 子代理，也能派 DeepSeek 子代理（`claude-deepseek -p`），两侧共用同一套任务名、判据、报告和打断/续跑协议；并把工具改名成 `sub-agent-runner`。

**Architecture:** `judge` **一行不改**。runner 的职责是把「本轮的产物」摆成 `judge` 已经认识的形状——报告文件 + 本轮日志文本。runner 之间真正不同的只有四件事：起跑的 argv+env、续跑的 argv、收尾时写不写报告、PID 反查的判据。

**Tech Stack:** Python 3 标准库（`re`/`json`/`os`/`pathlib`/`subprocess`/`uuid`），`unittest`。无第三方依赖，不许新增。

配套 spec：`docs/superpowers/specs/2026-09-23-sub-agent-runner-design.md`（201 行）。
**计划里任何说不清楚的地方去读 spec——那里每条决定都写了理由，而且全部依据都是 2026-09-23 的实测。**

## Global Constraints

- **`judge` 一行不改。** 任何「给 judge 加个 runner 分支」的冲动都是设计跑偏的信号
- **codex 那条路径行为一字不变**，包括 `_tee_until_exit` 的分块读（它那段注释说明了那是承重的）
- `--runner codex|deepseek` **必填**，不给缺省（仓库规范第 6 条）
- 三条硬拒绝一律**退 2 并说明为什么**，不静默改正
- deepseek 的 **session id 由工具生成**（`uuid.uuid4()`），落进元数据，此后每轮从元数据读；**绝不从输出里读回来核对**
- **代码里不许有「读不到 runner 就当 codex」的路径**——靠 Task 1 的一次性迁移
- 需要 HOME 沙箱的测试类一律继承 `_HomeSandbox`；patch 写 `mock.patch`；临时目录不加 rmtree
- 不设测试数量门槛（与仓库规范第 7 条「积极删除或合并」冲突）
- **测试里一次都不许真起 codex 或 claude**：`setUpModule` 的 `Popen.__init__` 断言要扩展到 `claude`
- 提交时**不要传** `-c user.email` / `-c user.name`，也不要设 `GIT_AUTHOR_*`

---

## File Structure

| 文件 | 职责 |
|---|---|
| `codex_sub_agent.py` → `sub_agent_runner.py` | 全部实现。单文件 CLI 是既有形态 |
| `test_codex_sub_agent.py` → `test_sub_agent_runner.py` | 全部测试 |
| `SKILL.md` / `README.md` | 文档 |

**改名放最后一个 Task**：前面几个 Task 在旧名字下做完，改名就是一个干净的、可单独审的 diff；反过来则整个开发过程都在追一个移动的目标。

---

### Task 1: 迁移现存 260 份元数据（先做，且是一次操作不是一段代码）

**Files:** 无代码改动。只跑一次脚本。

**为什么排第一：** `REQUIRED_META_KEYS = tuple(new_meta(...).keys())` —— 校验面从构造器派生。
Task 2 给 `new_meta` 加 `runner` 之后，**没有这个字段的旧元数据会被 `_load_meta` 当场拒掉**。
而先迁移是**向前兼容**的：现在的 `_load_meta` 只检查「缺字段」，多一个字段它不管。
顺序反了，这 260 个任务会有一段时间从 `status`/`resume`/`stop` 里全部消失。

- [ ] **Step 1: 先量一遍现状**

```bash
cd /home/xy/.claude/skills/codex-sub-agent
python3 -c "
import codex_sub_agent as ca, json
metas = ca.all_metas()
print('元数据总数:', len(metas))
print('已有 runner 字段的:', sum(1 for _, m in metas if 'runner' in m))
alive = [m['task'] for h, m in metas if ca.find_codex_pid(ca._report_path(h, m['task'])) is not None]
print('还在跑的:', alive or '无')
"
```
预期：总数 260 左右、已有 runner 的 0 个、**还在跑的为「无」**。
**如果有还在跑的，停下来**——迁移一个活任务的元数据会和它自己的写入撞车。

- [ ] **Step 2: 迁移，用 write_meta 而不是裸写**

```bash
python3 -c "
import codex_sub_agent as ca
done = skipped = 0
for home, meta in ca.all_metas():
    if 'runner' in meta:
        skipped += 1
        continue
    meta['runner'] = 'codex'          # 现存的每一个都是 codex 年代的
    ca.write_meta(home, meta['task'], meta)   # 原子替换，和生产路径同一个写法
    done += 1
print(f'补上 runner 的: {done}，本来就有的: {skipped}')
"
```

**用 `write_meta` 不用裸 `json.dump`**：它是原子替换（临时文件 + `os.replace`），
而 `status` 随时可能在另一个进程里读同一份 json。这条理由它自己的 docstring 里写了
（实测截断重写时 12284 次并发读里有 8277 次读到半截 json）。

- [ ] **Step 3: 核对，且要核对的是「数量对得上」而不是「脚本没报错」**

```bash
python3 -c "
import codex_sub_agent as ca
metas = ca.all_metas()
missing = [m['task'] for _, m in metas if 'runner' not in m]
wrong = [m['task'] for _, m in metas if m.get('runner') != 'codex']
print('总数:', len(metas), '| 仍缺 runner:', len(missing), '| 值不是 codex 的:', len(wrong))
assert not missing and not wrong, (missing[:5], wrong[:5])
print('OK')
"
./codex_sub_agent.py status 2>&1 | wc -l
```
预期：`仍缺 runner: 0`、`值不是 codex 的: 0`、`OK`；`status` 能列出全部任务。

- [ ] **Step 4: 不提交**（这一步没有代码改动）。把 Step 3 的输出记下来，Task 2 的提交信息里要引用。

---

### Task 2: runner 这条轴——四件事收拢，元数据加字段

**Files:**
- Modify: `codex_sub_agent.py` —— `new_meta`、`isolation_home`、`auth_source`、`ensure_isolation`、`find_codex_pid`、`build_run_argv`/`build_resume_argv` 的调用点
- Modify: `test_codex_sub_agent.py`

**Interfaces:**
- Produces: `RUNNERS = ("codex", "deepseek")`、`DEEPSEEK = "deepseek"`、`CODEX = "codex"`、
  `isolation_home(runner, account)`、`new_meta(task, runner, account, workdir, effort, skills)`

- [ ] **Step 1: 写失败的测试**

```python
class TestRunnerAxis(_HomeSandbox):
    def test_元数据记了runner(self):
        m = ca.new_meta("t", ca.CODEX, "default", "/tmp", "low", ())
        self.assertEqual(m["runner"], ca.CODEX)

    def test_deepseek的account必须是None(self):
        # 这个 runner 没有账号概念。给它一个账号名，就是让两件不相干的事共用一个字段。
        m = ca.new_meta("t", ca.DEEPSEEK, None, "/tmp", "max", ())
        self.assertIsNone(m["account"])
        with self.assertRaises(ValueError):
            ca.new_meta("t", ca.DEEPSEEK, "acct2", "/tmp", "max", ())

    def test_runner不在白名单就炸(self):
        with self.assertRaises(ValueError):
            ca.new_meta("t", "gpt4", "default", "/tmp", "low", ())

    def test_校验面仍然从构造器派生(self):
        # 加字段之后这条不变量必须还在：两份清单必然漂移，而漂移的后果是静默的
        self.assertIn("runner", ca.REQUIRED_META_KEYS)
        self.assertEqual(set(ca.REQUIRED_META_KEYS),
                         set(ca.new_meta("t", ca.CODEX, "default", "/tmp", "low", ())))

    def test_两个runner的隔离目录不同且deepseek没有账号维度(self):
        self.assertEqual(ca.isolation_home(ca.CODEX, "default"), self.home / ".codex-subagent")
        self.assertEqual(ca.isolation_home(ca.CODEX, "acct2"), self.home / ".codex-subagent-acct2")
        self.assertEqual(ca.isolation_home(ca.DEEPSEEK, None), self.home / ".claude-subagent")
        with self.assertRaises(ValueError):
            ca.isolation_home(ca.DEEPSEEK, "acct2")

    def test_隔离目录名一个都没改(self):
        # **改它会让 260 个现存任务从 status/resume/stop 里全部消失。**
        # 目录名是内部的，用户敲的是命令名。这条不对称是刻意的。
        self.assertEqual(ca.isolation_home(ca.CODEX, "default").name, ".codex-subagent")
        self.assertEqual(ca.isolation_home(ca.CODEX, "acct2").name, ".codex-subagent-acct2")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /home/xy/.claude/skills/codex-sub-agent
python3 -m unittest test_codex_sub_agent.TestRunnerAxis -v
```
预期：全部 ERROR（没有 `ca.CODEX` 等）。

- [ ] **Step 3: 实现**

在 `AUTO` / `DEFAULT_ACCOUNT` 旁边加：

```python
CODEX = "codex"
DEEPSEEK = "deepseek"
# runner 不是「模型」：换 runner 连隔离机制、报告怎么拿、有没有账号都不一样
# （codex 有多账号额度，deepseek 只有一个 token）。所以参数叫 --runner 不叫 --model。
RUNNERS = (CODEX, DEEPSEEK)
```

`isolation_home` 改签名：

```python
def isolation_home(runner, account):
    """这个任务住哪个隔离目录。**由 (runner, account) 一对值决定。**

    拆成两个参数让调用方各传各的，正是「好 API 难被误用」要消掉的那种缝：
    `account` 只有 codex 才有意义，deepseek 传任何非 `None` 都是调用方搞混了。

    **目录名一个都不改。** `~/.codex-subagent*` 是 260 份现存元数据里的归属，
    改名会让那些任务从 `status`／`resume`／`stop` 里全部消失——正是这个工具
    最该消灭的那种静默失效。命令名改成 `sub-agent-runner`，目录名不动，
    **这条不对称是刻意的**。
    """
    base = pathlib.Path.home()
    if runner == DEEPSEEK:
        if account is not None:
            raise ValueError(f"runner={DEEPSEEK} 没有账号概念，account 必须是 None，收到 {account!r}")
        return base / ".claude-subagent"
    if runner != CODEX:
        raise ValueError(f"runner 必须是 {RUNNERS} 之一，收到 {runner!r}")
    return base / ".codex-subagent" if account == DEFAULT_ACCOUNT else base / f".codex-subagent-{account}"
```

`new_meta` 加参数与校验（**校验写在构造器里**，这样 `REQUIRED_META_KEYS` 自动跟上）：

```python
def new_meta(task, runner, account, workdir, effort, skills):
    ...
    if runner not in RUNNERS:
        raise ValueError(f"runner 必须是 {RUNNERS} 之一，收到 {runner!r}")
    if runner == DEEPSEEK and account is not None:
        raise ValueError(f"runner={DEEPSEEK} 没有账号概念，account 必须是 None，收到 {account!r}")
    _require_skill_paths(skills)
    return {"task": task, "runner": runner, "account": account, "dir": workdir,
            "effort": effort, "skills": skills, "session_id": None,
            "started_at": _now_iso(), "writer_pid": writer_pid, "writer_start": writer_start}
```

`REQUIRED_META_KEYS` 那行跟着改成 `tuple(new_meta("", CODEX, DEFAULT_ACCOUNT, "", "", ()).keys())`。

`auth_source` 只对 codex 有意义——给它加一条断言而不是悄悄返回一个不存在的路径：

```python
def auth_source(account):
    """codex 账号的登录态在哪。**只有 codex 有这个概念**（deepseek 走环境变量里的 token）。"""
    ...
```

所有 `isolation_home(x)` 的调用点改成 `isolation_home(runner, account)`；
`accounts_by_availability` 里的 `isolation_home(a)` 改成 `isolation_home(CODEX, a)`
（它本来就只管 codex 的账号分配）。

- [ ] **Step 4: 跑全量**

```bash
python3 -m unittest test_codex_sub_agent 2>&1 | tail -3
```
预期：全绿。**改完之后再跑一次 Task 1 Step 3 的核对**，确认 260 份元数据仍然读得出来。

- [ ] **Step 5: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "feat: 元数据加 runner，isolation_home 改收 (runner, account)

「住在哪个隔离目录」现在由一对值决定。拆成两个参数各传各的，
正是「好 API 难被误用」要消掉的那种缝：account 只有 codex 才有意义。

校验写在构造器里，所以 REQUIRED_META_KEYS 自动跟上——那条
「校验面由构造器派生，不另写一份清单」的不变量原样保住。

隔离目录名一个都没改。改它会让现存 260 份元数据里的任务从
status/resume/stop 里全部消失。目录名是内部的，命令名才是用户敲的。
那 260 份已在本 Task 之前迁移完毕（补上 runner=codex，用 write_meta
原子替换，核对后 仍缺 0 / 值不对 0）。"
```

---

### Task 3: deepseek runner 的四件事

**Files:** Modify `codex_sub_agent.py`、`test_codex_sub_agent.py`

**Interfaces:**
- Produces: `DEEPSEEK_BIN = "claude-deepseek"`、`build_deepseek_argv(...)`、`deepseek_env(home)`、
  `finish_deepseek_round(round_text, report_path) -> None`、`find_agent_pid(runner, meta, report_path)`

**背景（实测，不是设想）：** `claude-deepseek` 是把 Claude Code 指向 DeepSeek 端点的透传包装，
白名单里只有 `deepseek-flash[1m]`。实跑确认：`CLAUDE_CONFIG_DIR` 指向空目录照跑；
`--session-id <我们生成的 uuid>` 原样回传、之后 `--resume <同一个 uuid>` 上下文完整；
SIGINT 后 **退出码是 0**（区分不了），但 `result` 事件的 `subtype` 是 `error_during_execution`。

- [ ] **Step 1: 写失败的测试**

```python
class TestDeepseekRunner(_HomeSandbox):
    RESULT_OK = ('{"type":"result","subtype":"success","is_error":false,'
                 '"result":"干完了","session_id":"%s","num_turns":2}')
    RESULT_INT = ('{"type":"result","subtype":"error_during_execution","is_error":true,'
                  '"result":"","session_id":"%s"}')

    def test_argv带我们自己生成的session_id(self):
        # session id 是工具拥有的，不从输出里读回来核对——那会把一个已知事实
        # 变成一个待验证的推测。它同时是 PID 反查的着力点（argv 里唯一的唯一串）。
        argv = ca.build_deepseek_argv("/tmp/wd", "max", "sid-123", "干活")
        self.assertIn("--session-id", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], "sid-123")
        self.assertEqual(argv[0], ca.DEEPSEEK_BIN)
        self.assertIn("-p", argv)

    def test_argv要stream_json且要verbose(self):
        # 实测：--output-format stream-json 不带 --verbose 会退 1
        #（"requires --verbose"）。而单块 json 中途被杀就什么都不剩。
        argv = ca.build_deepseek_argv("/tmp/wd", "max", "sid-123", "干活")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", argv)

    def test_续跑用resume不再传session_id(self):
        argv = ca.build_deepseek_resume_argv("/tmp/wd", "max", "sid-123", "接着干")
        self.assertEqual(argv[argv.index("--resume") + 1], "sid-123")
        self.assertNotIn("--session-id", argv)

    def test_隔离走CLAUDE_CONFIG_DIR(self):
        env = ca.deepseek_env(self.home / ".claude-subagent")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.home / ".claude-subagent"))

    def test_成功才写报告(self):
        # **这是 judge 一行不改的全部原因。** 现有那条「报告没出现＝没正常收尾」
        # 原样生效，前提是我们只在真成功时才把报告放上去。
        rp = self.home / "r.md"
        ca.finish_deepseek_round(self.RESULT_OK % "sid-1", rp)
        self.assertEqual(rp.read_text(), "干完了")

    def test_被打断不写报告(self):
        rp = self.home / "r.md"
        ca.finish_deepseek_round(self.RESULT_INT % "sid-1", rp)
        self.assertFalse(rp.exists())

    def test_失败时往日志里写一行judge认识的错误行(self):
        # judge 的 detail 来自 runtime_error_lines，而那套正则认的是 ^ERROR: 之类。
        # JSONL 每行以 { 开头，一条都不会命中——失败时 detail 会是空的。
        # 修法不是给 judge 加分支，是让 runner 写一行现有分类器原样认得的。
        line = ca.deepseek_error_line(self.RESULT_INT % "sid-1")
        self.assertTrue(line.startswith("ERROR: "))
        self.assertIn("error_during_execution", line)
        self.assertEqual(ca.runtime_error_lines(line), [line])
        self.assertIsNone(ca.deepseek_error_line(self.RESULT_OK % "sid-1"))

    def test_尾部坏行不许让解析崩(self):
        # 实测：进程被 SIGINT 杀掉时 JSONL 最后一行必然是残的；
        # status 读活日志时同样会撞到。
        torn = (self.RESULT_OK % "sid-1") + '\n{"type":"system","subty'
        rp = self.home / "r.md"
        ca.finish_deepseek_round(torn, rp)     # 不许抛
        self.assertEqual(rp.read_text(), "干完了")

    def test_没有result事件就不写报告(self):
        rp = self.home / "r.md"
        ca.finish_deepseek_round('{"type":"system","subtype":"init"}\n', rp)
        self.assertFalse(rp.exists())

    def test_PID反查按runner分叉且不用正则(self):
        # codex 侧那条注释记了实测事故：任务 a 的报告路径拿去 pgrep，
        # 命中了任务 aXmd（a + 任意字符 + md）。这里要的是相等，不是匹配。
        self.assertEqual(ca.agent_comm(ca.CODEX), b"codex")
        self.assertEqual(ca.agent_comm(ca.DEEPSEEK), b"claude")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m unittest test_codex_sub_agent.TestDeepseekRunner -v
```

- [ ] **Step 3: 实现**

```python
DEEPSEEK_BIN = "claude-deepseek"
# 这个 runner 只接受 max。用户的硬约束，写成常量而不是散落的字面量。
DEEPSEEK_EFFORT = "max"


def deepseek_env(home):
    """deepseek 侧的隔离：`CLAUDE_CONFIG_DIR`。

    实测指向空目录照跑——auth 走环境变量里的 token，不在配置目录里，
    所以空目录正是我们要的隔离：子代理一个 skill、一个 MCP、一个 hook 都看不见。
    """
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    return env


def _deepseek_base_argv(dir_abs, effort):
    # --output-format stream-json 实测强制要 --verbose（不带就退 1）。
    # 而不用单块 json：它只在结束时吐一次，中途被打断日志里什么都不剩，
    # 而那正是这个工具存在的理由。
    return [DEEPSEEK_BIN, "-p", "--effort", effort,
            "--output-format", "stream-json", "--verbose",
            "--add-dir", dir_abs]


def build_deepseek_argv(dir_abs, effort, session_id, brief):
    """第一轮：**session id 由我们指定**，不是等它生成再抠出来。

    实测 `--session-id <uuid>` 原样回传，之后 `--resume <同一个 uuid>` 上下文完整。
    这样做有两个好处，第二个是承重的：
    ① 少一条从文本里捞事实的路（codex 侧的 session id 是拿正则从日志里抠的，
       而日志混着 brief 原文和子进程输出——这个仓库反复在修的正是这一类）
    ② **PID 反查有了着力点**：`find_codex_pid` 靠「报告路径是独立 argv 元素」，
       而 `claude -p` 的 argv 里根本没有报告路径。uuid 顶上那个位置。
    """
    return _deepseek_base_argv(dir_abs, effort) + ["--session-id", session_id, brief]


def build_deepseek_resume_argv(dir_abs, effort, session_id, brief):
    return _deepseek_base_argv(dir_abs, effort) + ["--resume", session_id, brief]


def _last_result_event(round_text):
    """本轮 JSONL 里最后一条 `result` 事件；没有就 `None`。

    **逐行 try/except，坏行跳过。** 实测：进程被 SIGINT 杀掉时最后一行必然是残的，
    而 `status` 读活日志时同样会撞到——一个残行让整条判据抛异常是不可接受的。
    """
    found = None
    for line in round_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            found = event
    return found


def finish_deepseek_round(round_text, report_path):
    """**只在真成功时才把报告放上去。**

    这是 `judge` 一行不改的全部原因：现有那条「报告没出现＝没正常收尾」原样生效，
    而 deepseek 的成败是结构化的（`subtype`），比 codex 那侧从日志里推可靠得多。
    把结构化信号用在这里、而不是给 judge 加一条分支，判据就仍然只有一个家。
    """
    event = _last_result_event(round_text)
    if event is None or event.get("subtype") != "success":
        return
    report_path.write_text(event.get("result") or "")


def deepseek_error_line(round_text):
    """失败时给日志补一行 `judge` 已经认识的错误行；成功或没结果就 `None`。

    `judge` 的 `detail` 来自 `runtime_error_lines`，而那套正则认的是
    `^ERROR:` / tracing / `^Error:`。JSONL 每行以 `{` 开头，**一条都不会命中**
    ——不补这一行，deepseek 失败时调用方看到的 detail 是空的。
    补一行现有分类器原样认得的，比给 judge 加分支好：判据还是一个家。
    """
    event = _last_result_event(round_text)
    if event is None or event.get("subtype") == "success":
        return None
    detail = (event.get("result") or "").strip().splitlines()
    return f"ERROR: {event.get('subtype')}: {detail[0] if detail else '没有更多信息'}"


def agent_comm(runner):
    """这个 runner 的进程在 `/proc/<pid>/comm` 里叫什么。

    实测：`claude-deepseek` 是 `exec` 掉的 bash 包装，所以真实进程的 comm 是 `claude`。
    """
    return b"codex" if runner == CODEX else b"claude"
```

`find_codex_pid` 改名成 `find_agent_pid(runner, needle, )`——**保留「按 argv 元素精确比对、
绝不用正则」这条**，只把 `comm` 和 needle 变成按 runner 取：

```python
def find_agent_pid(runner, needle):
    """存活判定只有一个可靠判据：真实 PID。（原 `find_codex_pid`，理由原样保留。）

    `needle` 是**这个 runner 的 argv 里那个工具自己拥有的唯一串**：
    codex 是报告路径（`-o <路径>`），deepseek 是 session id uuid（`--session-id <uuid>`）。
    两侧都按 **argv 元素精确比对**，一点正则语义都不引入——codex 侧那条注释记了实测事故：
    任务 `a` 的报告路径拿去 pgrep，命中了任务 `aXmd`（`a`+任意字符+`md`）。
    """
```

- [ ] **Step 4: 跑全量**

```bash
python3 -m unittest test_codex_sub_agent 2>&1 | tail -3
```

- [ ] **Step 5: 提交**

---

### Task 4: tee 的行模式 + `thinking_tokens` 过滤

**Files:** Modify `codex_sub_agent.py` 的 `_tee_until_exit`、`test_codex_sub_agent.py`

**背景（实测）：** `thinking_tokens` 事件**每 token 一条**——8 秒 2320 条、462 KB，约 **58 KB/s**。
一个 30 分钟的任务约 **104 MB**，而判据要扫日志。没有任何开关能关掉它
（`stream-json` 还强制要 `--verbose`）。本次写 spec 期间派出的那个 DeepSeek 审查子代理，
日志跑到了 **57,365 行**——这条不是设想。

- [ ] **Step 1: 写失败的测试**

```python
class TestTeeLineMode(_HomeSandbox):
    def test_codex那条路径仍然是分块读(self):
        # **这条是护栏。** codex 的 tee 注释说明了分块读是承重的：
        # banner 只有 ~170 字节，之后可能思考几十分钟，按行读会把 session id
        # 卡在缓冲里，包装进程此时被杀这一轮就再也 resume 不回来。
        self.assertIsNone(ca.log_line_filter(ca.CODEX))

    def test_deepseek滤掉thinking_tokens(self):
        keep = ca.log_line_filter(ca.DEEPSEEK)
        self.assertFalse(keep('{"type":"system","subtype":"thinking_tokens","n":1}'))
        self.assertTrue(keep('{"type":"result","subtype":"success"}'))
        self.assertTrue(keep('{"type":"assistant"}'))
        self.assertTrue(keep('{"type":"system","subtype":"init"}'))

    def test_坏行要留着(self):
        # 残行是现场证据。滤掉它等于把「这一轮被杀在半路」这个事实也抹掉。
        keep = ca.log_line_filter(ca.DEEPSEEK)
        self.assertTrue(keep('{"type":"system","subty'))

    def test_行模式下半行会被攒着而不是切坏(self):
        chunks = [b'{"type":"assistant"}\n{"ty', b'pe":"result"}\n']
        got = ca.split_complete_lines(chunks)
        self.assertEqual(got, ['{"type":"assistant"}', '{"type":"result"}'])
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

```python
# 本轮日志已滤掉 thinking_tokens 进度事件。丢弃是有损的，所以要说破——
# 不写这一句，读日志的人会以为工具漏记了。
DEEPSEEK_LOG_NOTE = ("===== 本轮日志已滤掉 claude 的 thinking_tokens 进度事件"
                     "（每 token 一条，实测约 58 KB/s）。其余事件一字未改。 =====\n")


def log_line_filter(runner):
    """这个 runner 的日志行过滤器；`None` 表示不过滤、走原来的分块读。

    **codex 侧必须是 `None`。** 它的 tee 注释说明了分块读是承重的：
    banner 只有 ~170 字节，之后可能思考几十分钟，按行读会把 session id 卡在
    缓冲里，而包装进程此时被杀，这一轮就再也 resume 不回来。

    deepseek 侧滤掉 `thinking_tokens`：实测 8 秒 2320 条、462 KB（约 58 KB/s），
    30 分钟的任务约 104 MB，而判据要扫日志。没有开关能关掉它。
    **坏行要留着**——残行是「这一轮被杀在半路」的现场证据。
    """
    if runner != DEEPSEEK:
        return None
    def keep(line):
        line = line.strip()
        if not line.startswith("{"):
            return True
        try:
            event = json.loads(line)
        except ValueError:
            return True            # 坏行留着，它是证据
        return not (event.get("type") == "system"
                    and event.get("subtype") == "thinking_tokens")
    return keep
```

`_tee_until_exit` 加行模式分支。**codex 那一支一个字节都不动**，行模式是另一条路。

- [ ] **Step 4: 跑全量 + 拿真实数据验一次**

```bash
python3 -m unittest test_codex_sub_agent 2>&1 | tail -3
python3 -c "
import codex_sub_agent as ca, pathlib, glob
keep = ca.log_line_filter(ca.DEEPSEEK)
f = sorted(glob.glob('/home/xy/.claude/jobs/*/tmp/ds-review/out.jsonl'))
if not f: print('没有现场日志可验，跳过'); raise SystemExit
raw = pathlib.Path(f[-1]).read_text(errors='replace').splitlines()
kept = [l for l in raw if keep(l)]
print(f'原始 {len(raw)} 行 / {sum(len(l) for l in raw)} 字节')
print(f'滤后 {len(kept)} 行 / {sum(len(l) for l in kept)} 字节')
print(f'压掉 {100 - 100*len(kept)/len(raw):.1f}%')
"
```
预期：压掉 95% 以上。**这一步用的是真实现场日志，不是 fixture**——
模块头那份「空测试形态」清单第 8 条说的就是 fixture 经过了我们的手。

- [ ] **Step 5: 提交**

---

### Task 5: CLI —— `--runner` 与三条硬拒绝

**Files:** Modify `codex_sub_agent.py` 的 `build_parser`、`cmd_run`、`cmd_resume`、`cmd_status`、`cmd_stop`、`run_codex`；`test_codex_sub_agent.py`

- [ ] **Step 1: 写失败的测试**

```python
class TestRunnerCLI(_HomeSandbox):
    def test_runner必填(self):
        with self.assertRaises(SystemExit):
            ca.build_parser().parse_args(["run", "--task", "t", "--dir", "/tmp",
                                          "--brief", "/tmp/b.md", "--effort", "low",
                                          "--account", "default", "--no-skill"])

    def test_deepseek给account当场拒(self):
        # 不静默忽略：忽略等于让调用方以为自己选了个账号，而那句话是假的。
        with self.assertRaises(ca.Rejected) as got:
            ca.cmd_run(self._args(runner="deepseek", account="acct2", effort="max"))
        self.assertIn("账号", got.exception.message)

    def test_deepseek的effort不是max就拒(self):
        # 用户的硬约束。不静默改成 max——claude-deepseek 自己就是这么干的
        #（模型名不在白名单就退 2，注释写「静默跑错模型变成大声退 2」）。
        with self.assertRaises(ca.Rejected) as got:
            ca.cmd_run(self._args(runner="deepseek", account=None, effort="high"))
        self.assertIn("max", got.exception.message)

    def test_deepseek的effort是max就放行(self):
        with _no_agent() as popen:
            ca.cmd_run(self._args(runner="deepseek", account=None, effort="max"))
        self.assertTrue(popen.called)

    def test_codex仍然必须给account(self):
        with self.assertRaises(ca.Rejected):
            ca.cmd_run(self._args(runner="codex", account=None, effort="low"))

    def test_resume_stop_status都不收runner(self):
        # runner 从元数据查出来，和 account 同一个模式，不可能指错
        for cmd in ("resume", "stop"):
            with self.assertRaises(SystemExit):
                ca.build_parser().parse_args([cmd, "t", "--runner", "codex"])
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 改 parser**

```python
    r.add_argument("--runner", required=True, choices=list(RUNNERS),
                   help="派给哪个 agent 跑")
```

`--account` 改成**可选**（deepseek 侧必须不给），并在 `cmd_run` 里按 runner 校验——
**校验放在代码里而不是靠 argparse**，因为 argparse 表达不了「A 必填当且仅当 B 是某值」，
而把它写成文档约定正是仓库规范第 5 条要消灭的东西。

`cmd_run` 里加三条拒绝（每条都要说清**为什么**，不只是「不允许」）。

- [ ] **Step 4: 跑全量**

- [ ] **Step 5: 真跑一次冒烟**

```bash
cd /home/xy/.claude/skills/codex-sub-agent
printf '回答两个字：好的\n' > /tmp/smoke-ds.md
./codex_sub_agent.py run --task smoke-deepseek --runner deepseek --dir /tmp \
  --brief /tmp/smoke-ds.md --effort max --no-skill
echo "退出码 $?"
./codex_sub_agent.py status smoke-deepseek
```
预期：`success`、退出码 0、`status` 能列出来且 runner 列显示 deepseek。
**codex 侧不要跑冒烟**——三个账号全部限流（acct3 到 9/25 17:04、default 到 9/26 17:47、
acct2 到 9/28 05:53）。

- [ ] **Step 6: 提交**

---

### Task 6: 改名 `codex-sub-agent` → `sub-agent-runner`

**Files:** 目录、两个 `.py`、`SKILL.md`、`README.md`、`~/.claude/settings.json`

**放最后的理由：** 前面几个 Task 在旧名字下做完，改名就是一个干净的、可单独审的 diff。

- [ ] **Step 1: 先查清楚有哪些地方引用了旧名字**

```bash
grep -rn "codex-sub-agent\|codex_sub_agent" ~/.claude/settings.json ~/.claude/CLAUDE.md \
  /home/xy/.claude/skills/codex-sub-agent/{SKILL.md,README.md} 2>/dev/null | head -20
```

- [ ] **Step 2: 移动目录与文件（用 `git mv`，保住历史）**

```bash
cd /home/xy/.claude/skills/codex-sub-agent
git mv codex_sub_agent.py sub_agent_runner.py
git mv test_codex_sub_agent.py test_sub_agent_runner.py
sed -i 's/codex_sub_agent/sub_agent_runner/g' sub_agent_runner.py test_sub_agent_runner.py
sed -i 's/codex-sub-agent/sub-agent-runner/g' sub_agent_runner.py test_sub_agent_runner.py SKILL.md README.md
python3 -m unittest test_sub_agent_runner 2>&1 | tail -3
git commit -am "refactor: 改名 codex-sub-agent → sub-agent-runner"
cd .. && mv codex-sub-agent sub-agent-runner
```

**目录移动放在提交之后**：移动一个 git 工作区的根目录不影响仓库本身，但要先让提交落地。

- [ ] **Step 3: 更新外部引用**

```bash
grep -rn "codex-sub-agent" ~/.claude/settings.json ~/.claude/CLAUDE.md 2>/dev/null
# 逐个改掉；改完再 grep 一次确认为 0
```

- [ ] **Step 4: 核对隔离目录一个都没改**

```bash
ls -d ~/.codex-subagent* ~/.claude-subagent 2>/dev/null
python3 -c "
import sub_agent_runner as ca
print('元数据仍然读得出:', len(ca.all_metas()))"
```
预期：`~/.codex-subagent*` 原样在；元数据数量和 Task 1 量到的一致。

- [ ] **Step 5: 文档**

`SKILL.md` 的账号那一格拆成 runner + 账号两格；README 同步。
**`--runner deepseek` 的 effort 只能是 max 这件事要写在文档里**，但
**不要**写成「记得传 max」——代码已经硬拒绝了，文档只说事实。

- [ ] **Step 6: 提交**

---

## Self-Review 结果

**Spec 覆盖**（逐条核对 spec 的 11 条验收判据）：

| spec 判据 | Task |
|---|---|
| 1 改名、隔离目录不改名 | Task 6（Step 4 专门核对） |
| 2 三条硬拒绝且说清为什么 | Task 5 |
| 3 两侧共用 brief/skill，`prepend_skill_guard` 未改 | Task 3/5（全程不碰那个函数） |
| 4 deepseek 判据：success 才写报告 | Task 3 |
| 5 日志过滤在 tee、codex 路径未改、坏行不崩 | Task 4（含真实现场日志验证） |
| 6 失败时 detail 不空，且不给 judge 加分支 | Task 3 的 `deepseek_error_line` |
| 7 续跑（含打断后） | Task 3 的 resume argv + Task 5 冒烟 |
| 8 元数据加字段 + 260 份迁移完 | Task 1 + Task 2 |
| 9 PID 反查两侧都准、不用正则 | Task 3 的 `find_agent_pid` |
| 10 既有测试全绿、codex 行为未变 | 每个 Task 的「跑全量」+ Task 4 的护栏测试 |
| 11 收尾内化并删文档 | 工作流程第 8 步，本计划之外 |

**无遗漏。**

**占位符扫描：** 无 TBD/TODO；每个代码步骤都有可粘贴的真代码；每条测试都有真断言。

**类型一致性：**
- `isolation_home(runner, account)`（Task 2）→ Task 3/5 按这个签名调用 —— 一致
- `new_meta(task, runner, account, workdir, effort, skills)`（Task 2）→ Task 5 按此传参 —— 一致
- `finish_deepseek_round(round_text, report_path)` / `deepseek_error_line(round_text)`（Task 3）
  收同一种输入（本轮日志文本）—— 一致，且这正是 C1 那类 bug 的反面
- `log_line_filter(runner) -> callable | None`（Task 4）→ tee 按 `None` 与否分支 —— 一致
