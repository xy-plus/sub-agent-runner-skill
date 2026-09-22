# sub-agent-runner Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 同一个工具既能派 codex 子代理也能派 DeepSeek 子代理，共用同一套任务名、判据、报告和打断/续跑协议；并把工具改名成 `sub-agent-runner`。

**Architecture:** `judge` **一行不改**。runner 的职责是把「本轮的产物」摆成 `judge` 已经认识的形状——报告文件 + 本轮日志文本。runner 之间真正分叉的点有 **7 个**，spec 第九节给了完整的调用点清单。

**Tech Stack:** Python 3 标准库（`re`/`json`/`os`/`pathlib`/`subprocess`/`uuid`），`unittest`。不许新增依赖。

配套 spec：`docs/superpowers/specs/2026-09-23-sub-agent-runner-design.md`（v2，255 行）。
**每条决定的理由都在 spec 里，全部依据是 2026-09-23 的实测。说不清楚就去读它。**

> **v2**：v1 被一个 DeepSeek 子代理的 spec 审查推翻了几处，最严重的是 **v1 的 Task 6 里那条全局 `sed` 会毁掉盘上契约**（见 Task 6）。

## Global Constraints

- **`judge` 一行不改。** 想给它加 runner 分支＝设计跑偏了
- **codex 那条路径行为一字不变**，包括 `_tee_until_exit` 的分块读
- **盘上契约一个都不许改**：`~/.codex-subagent*` 目录名、`ROUND_MARK`、`INTERRUPT_MARK` 的文本
- `--runner codex|deepseek` 必填，不给缺省
- 三条硬拒绝退 2 并说清为什么，**且排在任何状态变更之前**
- deepseek 的 session id 由工具生成（`uuid.uuid4()`），**必须是独立 argv 元素**，不许 `=` 连接
- 代码里不许有「读不到 runner 就当 codex」的路径
- **测试一次都不许真起 codex 或 claude**；`setUpModule` 的断言要扩到 `claude-deepseek`
- 需要 HOME 沙箱的测试类继承 `_HomeSandbox`；patch 写 `mock.patch`；临时目录不加 rmtree
- 不设测试数量门槛
- 提交不传 `-c user.email` / `-c user.name`

---

### Task 1: 迁移现存 260 份元数据（先做；是一次操作，不是一段代码）

**为什么排第一：** `REQUIRED_META_KEYS = tuple(new_meta(...).keys())`。Task 2 加 `runner` 之后，
没有这个字段的旧元数据会被 `_load_meta` 当场拒掉。先迁移是**向前兼容**的（现在的
`_load_meta` 只查缺字段，多一个它不管）。顺序反了，260 个任务会有一段时间全部消失。

- [ ] **Step 1: 量现状，并确认没有活任务**

```bash
cd /home/xy/.claude/skills/codex-sub-agent
python3 -c "
import codex_sub_agent as ca
metas = ca.all_metas()
print('总数:', len(metas), '| 已有 runner:', sum(1 for _, m in metas if 'runner' in m))
alive = [m['task'] for h, m in metas if ca.find_codex_pid(ca._report_path(h, m['task'])) is not None]
print('还在跑:', alive or '无')
assert not alive, '有活任务，停下——迁移会和它自己的写入撞车'
"
```

- [ ] **Step 2: 迁移，用 `write_meta`（原子替换）**

```bash
python3 -c "
import codex_sub_agent as ca
done = skipped = 0
for home, meta in ca.all_metas():
    if 'runner' in meta: skipped += 1; continue
    meta['runner'] = 'codex'
    ca.write_meta(home, meta['task'], meta)
    done += 1
print(f'补上: {done}，本来就有: {skipped}')
"
```

不用裸 `json.dump`：`write_meta` 是临时文件 + `os.replace`，而 `status` 随时可能在另一个
进程里读同一份 json（它的 docstring 记了实测：12284 次并发读里 8277 次读到半截）。

- [ ] **Step 3: 核对数量，不是核对「脚本没报错」**

```bash
python3 -c "
import codex_sub_agent as ca
metas = ca.all_metas()
missing = [m['task'] for _, m in metas if 'runner' not in m]
wrong = [m['task'] for _, m in metas if m.get('runner') != 'codex']
print('总数:', len(metas), '| 仍缺:', len(missing), '| 值不对:', len(wrong))
assert not missing and not wrong
print('OK')
"
```
把总数记下来，Task 2 的提交信息要引用。**不提交**（这一步没有代码改动）。

---

### Task 2: runner 这条轴

**Files:** Modify `codex_sub_agent.py`（`new_meta`／`REQUIRED_META_KEYS`／`isolation_home`／
`auth_source`／`find_meta`／`all_metas`／`ensure_isolation` 及其调用点）、`test_codex_sub_agent.py`

**Produces:** `CODEX`／`DEEPSEEK`／`RUNNERS`、`isolation_home(runner, account)`、
`new_meta(task, runner, account, workdir, effort, skills)`、`isolation_roots()`

- [ ] **Step 1: 写失败的测试**

```python
class TestRunnerAxis(_HomeSandbox):
    def test_元数据记了runner(self):
        self.assertEqual(ca.new_meta("t", ca.CODEX, "default", "/tmp", "low", ())["runner"], ca.CODEX)

    def test_deepseek的account必须是None(self):
        # 不许记一个假名字：account 进 status 第二列，而且 _resume_round 会拿它去
        # ensure_isolation——记 "deepseek" 会指向 ~/.codex-subagent-deepseek 这个
        # 不存在的账号目录，并因缺 auth 拒跑。
        self.assertIsNone(ca.new_meta("t", ca.DEEPSEEK, None, "/tmp", "max", ())["account"])
        with self.assertRaises(ValueError):
            ca.new_meta("t", ca.DEEPSEEK, "acct2", "/tmp", "max", ())

    def test_runner不在白名单就炸(self):
        with self.assertRaises(ValueError):
            ca.new_meta("t", "gpt4", "default", "/tmp", "low", ())

    def test_校验面仍然从构造器派生(self):
        self.assertIn("runner", ca.REQUIRED_META_KEYS)
        self.assertEqual(set(ca.REQUIRED_META_KEYS),
                         set(ca.new_meta("t", ca.CODEX, "default", "/tmp", "low", ())))

    def test_隔离目录由runner和account一对值决定(self):
        self.assertEqual(ca.isolation_home(ca.CODEX, "default"), self.home / ".codex-subagent")
        self.assertEqual(ca.isolation_home(ca.CODEX, "acct2"), self.home / ".codex-subagent-acct2")
        self.assertEqual(ca.isolation_home(ca.DEEPSEEK, None), self.home / ".claude-subagent")
        with self.assertRaises(ValueError):
            ca.isolation_home(ca.DEEPSEEK, "acct2")

    def test_隔离目录名一个都没改(self):
        # 改它会让 260 份现存元数据里的任务从 status/resume/stop 里全部消失。
        self.assertEqual(ca.isolation_home(ca.CODEX, "default").name, ".codex-subagent")
        self.assertEqual(ca.isolation_home(ca.CODEX, "acct2").name, ".codex-subagent-acct2")

    def test_扫描根包含deepseek的家(self):
        # **不加这一条，deepseek 任务对 status/resume/stop 整体不可见。**
        roots = ca.isolation_roots()
        self.assertIn(ca.isolation_home(ca.DEEPSEEK, None), roots)
        for a in ca.account_choices():
            self.assertIn(ca.isolation_home(ca.CODEX, a), roots)

    def test_deepseek的任务status查得到(self):
        home = ca.isolation_home(ca.DEEPSEEK, None)
        (home / "tasks").mkdir(parents=True)
        ca.write_meta(home, "dst", ca.new_meta("dst", ca.DEEPSEEK, None, "/tmp", "max", ()))
        got_home, got_meta = ca.find_meta("dst")
        self.assertEqual(got_home, home)
        self.assertEqual(got_meta["runner"], ca.DEEPSEEK)
        self.assertIn("dst", [m["task"] for _, m in ca.all_metas()])
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m unittest test_codex_sub_agent.TestRunnerAxis -v
```

- [ ] **Step 3: 实现**

在 `AUTO` / `DEFAULT_ACCOUNT` 旁边：

```python
CODEX = "codex"
DEEPSEEK = "deepseek"
# runner 不是「模型」：换 runner 连隔离机制、工作目录怎么传、报告怎么拿、
# 有没有账号都不一样。所以参数叫 --runner 不叫 --model。
RUNNERS = (CODEX, DEEPSEEK)
```

```python
def isolation_home(runner, account):
    """这个任务住哪个隔离目录。**由 (runner, account) 一对值决定。**

    拆成两个参数各传各的，正是「好 API 难被误用」要消掉的那种缝：
    `account` 只有 codex 才有意义，deepseek 传任何非 `None` 都是调用方搞混了。

    **目录名一个都不改。** `~/.codex-subagent*` 是 260 份现存元数据里的归属。
    命令名改成 `sub-agent-runner`，目录名不动——**这条不对称是刻意的**：
    用户敲的是命令名，盘上的东西谁都不该动。
    """
    base = pathlib.Path.home()
    if runner == DEEPSEEK:
        if account is not None:
            raise ValueError(f"runner={DEEPSEEK} 没有账号概念，account 必须是 None，收到 {account!r}")
        return base / ".claude-subagent"
    if runner != CODEX:
        raise ValueError(f"runner 必须是 {RUNNERS} 之一，收到 {runner!r}")
    return base / ".codex-subagent" if account == DEFAULT_ACCOUNT else base / f".codex-subagent-{account}"


def isolation_roots():
    """所有可能住着任务的隔离目录。**`find_meta` 和 `all_metas` 都走这里。**

    原来它们各自写 `for account in account_choices(): isolation_home(account)`，
    而 deepseek 的家不对应任何账号——**不收进来，deepseek 任务对
    `status`／`resume`／`stop` 整体不可见**，而那是静默的：任务跑着、报告在盘上，
    工具却说没有这个任务。
    """
    return [isolation_home(CODEX, a) for a in account_choices()] + [isolation_home(DEEPSEEK, None)]
```

`new_meta` 加参数与校验（校验写在构造器里，`REQUIRED_META_KEYS` 自动跟上）：

```python
def new_meta(task, runner, account, workdir, effort, skills):
    if runner not in RUNNERS:
        raise ValueError(f"runner 必须是 {RUNNERS} 之一，收到 {runner!r}")
    if runner == DEEPSEEK and account is not None:
        raise ValueError(f"runner={DEEPSEEK} 没有账号概念，account 必须是 None，收到 {account!r}")
    _require_skill_paths(skills)
    ...
    return {"task": task, "runner": runner, "account": account, "dir": workdir,
            "effort": effort, "skills": skills, "session_id": None,
            "started_at": _now_iso(), "writer_pid": writer_pid, "writer_start": writer_start}
```

`find_meta` / `all_metas` 里那两个 `for account in account_choices()` 循环改成
`for home in isolation_roots()`。

`ensure_isolation` 按 runner 分叉：deepseek 侧只建 `tasks`/`reports`/`logs`
（**少一个就是 tee 循环里的 `FileNotFoundError`**，代码里那条注释记着 codex 为此连踩两次），
不查 `auth.json`／`config.toml`／共享扫描根（那三条都是 codex 专属）。

- [ ] **Step 4: 跑全量 + 重跑 Task 1 Step 3 的核对**

- [ ] **Step 5: 提交**

---

### Task 3: deepseek runner 的七件事

**Files:** Modify `codex_sub_agent.py`、`test_codex_sub_agent.py`

**Produces:** `DEEPSEEK_BIN`／`build_deepseek_argv`／`build_deepseek_resume_argv`／
`deepseek_env`／`finish_deepseek_round`／`deepseek_log_lines`／`agent_comm`／`find_agent_pid`

- [ ] **Step 1: 写失败的测试**

```python
class TestDeepseekRunner(_HomeSandbox):
    OK = ('{"type":"result","subtype":"success","is_error":false,'
          '"result":"干完了","session_id":"sid-1","permission_denials":[]}')
    INT = ('{"type":"result","subtype":"error_during_execution","is_error":true,'
           '"result":"","session_id":"sid-1","permission_denials":[]}')
    DENIED = ('{"type":"result","subtype":"success","is_error":false,"result":"我写不进去",'
              '"session_id":"sid-1","permission_denials":'
              '[{"tool_name":"Write"},{"tool_name":"Bash"}]}')

    def test_uuid是独立argv元素不是等号连接(self):
        # **承重。** PID 反查按 argv 元素精确比对（不是正则——codex 那条注释记了
        # 实测事故：任务 a 的报告路径拿去 pgrep 命中了 aXmd）。写成 --session-id=<uuid>
        # 就成了一个元素，精确匹配静默失配 → status 说不在跑、stop 不发信号、
        # run 放行第二轮 → 两个写者共写一份日志。
        argv = ca.build_deepseek_argv("/tmp/wd", "max", "sid-123", "干活")
        self.assertIn("--session-id", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], "sid-123")
        self.assertNotIn("--session-id=sid-123", argv)
        rargv = ca.build_deepseek_resume_argv("/tmp/wd", "max", "sid-123", "接着干")
        self.assertEqual(rargv[rargv.index("--resume") + 1], "sid-123")
        self.assertNotIn("--resume=sid-123", rargv)
        self.assertNotIn("--session-id", rargv)

    def test_argv要带权限开关(self):
        # 不给的话子代理一个字都写不成，而这一轮仍然报 success——
        # 实测 permission_denials 三条全拒、工作目录为空。
        # 这正是 --skill 那次立项要杀的「零工作量的成功」。
        self.assertIn("--dangerously-skip-permissions",
                      ca.build_deepseek_argv("/tmp/wd", "max", "s", "b"))

    def test_argv要stream_json且要verbose(self):
        # 实测 stream-json 不带 --verbose 退 1；而单块 json 中途被杀就什么都不剩。
        argv = ca.build_deepseek_argv("/tmp/wd", "max", "s", "b")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", argv)

    def test_argv里没有工作目录_它走cwd(self):
        # claude -p 没有 --cd 等价物。工作目录必须由 Popen(cwd=) 交过去，
        # 否则子代理在包装器的 cwd 里干活，而元数据说的是 --dir——元数据说谎。
        self.assertNotIn("--cd", ca.build_deepseek_argv("/tmp/wd", "max", "s", "b"))

    def test_成功才写报告(self):
        rp = self.home / "r.md"
        ca.finish_deepseek_round(self.OK, rp)
        self.assertEqual(rp.read_text(), "干完了")

    def test_被打断不写报告(self):
        rp = self.home / "r.md"
        ca.finish_deepseek_round(self.INT, rp)
        self.assertFalse(rp.exists())

    def test_失败时补一行judge认识的错误行(self):
        lines = ca.deepseek_log_lines(self.INT)
        self.assertTrue(any(l.startswith("ERROR: ") for l in lines))
        self.assertIn("error_during_execution", "\n".join(lines))
        self.assertEqual(ca.runtime_error_lines("\n".join(lines)), lines)

    def test_工具被拒时也补一行_于是judge给suspect(self):
        # 报告在（它确实收尾了）+ 有错误行 → suspect → 退 3「要人看一眼」。
        # 用的是现有状态机，零新分支。
        rp = self.home / "r.md"
        ca.finish_deepseek_round(self.DENIED, rp)
        self.assertTrue(rp.exists())
        lines = ca.deepseek_log_lines(self.DENIED)
        self.assertTrue(any("被拒" in l for l in lines))
        v = ca.judge(ca.Round(rp, "\n".join(lines)))
        self.assertEqual(v.state, "suspect")

    def test_一切正常时不补任何行(self):
        self.assertEqual(ca.deepseek_log_lines(self.OK), [])

    def test_尾部坏行不许让解析崩(self):
        # 实测：进程被 SIGINT 杀掉时最后一行必然是残的；status 读活日志也会撞到。
        torn = self.OK + '\n{"type":"system","subty'
        rp = self.home / "r.md"
        ca.finish_deepseek_round(torn, rp)
        self.assertEqual(rp.read_text(), "干完了")
        self.assertEqual(ca.deepseek_log_lines(torn), [])

    def test_没有result事件就不写报告(self):
        rp = self.home / "r.md"
        ca.finish_deepseek_round('{"type":"system","subtype":"init"}\n', rp)
        self.assertFalse(rp.exists())

    def test_PID反查的进程名按runner取(self):
        # 实测读 /proc/<pid>/comm：claude-deepseek 是 exec 掉的 bash 包装，
        # 所以真实进程的 comm 是 claude，没有多一层。
        self.assertEqual(ca.agent_comm(ca.CODEX), b"codex")
        self.assertEqual(ca.agent_comm(ca.DEEPSEEK), b"claude")
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

```python
DEEPSEEK_BIN = "claude-deepseek"
DEEPSEEK_EFFORT = "max"          # 用户的硬约束，写成常量不散落字面量


def deepseek_env(home):
    """deepseek 侧的隔离：`CLAUDE_CONFIG_DIR`。

    实测指向空目录照跑——auth 走环境变量里的 token，不在配置目录里，
    所以空目录正是我们要的隔离：子代理一个 skill、一个 MCP、一个 hook 都看不见。
    """
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    return env


def _deepseek_base_argv(effort):
    # --output-format stream-json 实测强制要 --verbose（不带退 1）。
    # 不用单块 json：它只在结束时吐一次，中途被打断日志里什么都不剩。
    # --dangerously-skip-permissions 对应 codex 侧的 --sandbox danger-full-access
    # + approval_policy="never"。**不给的话子代理一个字都写不成，而这一轮仍报 success**
    # （实测 permission_denials 三条全拒、工作目录为空）。
    # **工作目录不在 argv 里**：claude -p 没有 --cd 等价物，它走 Popen(cwd=)。
    return [DEEPSEEK_BIN, "-p", "--effort", effort,
            "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions"]


def build_deepseek_argv(dir_abs, effort, session_id, brief):
    """第一轮。**session id 由我们指定，且必须是独立 argv 元素。**

    `--session-id=<uuid>` 会并成一个元素，让 `find_agent_pid` 的精确比对静默失配。
    `dir_abs` 收在签名里是**刻意的**：调用方不必去分辨「这个 runner 的工作目录
    走 argv 还是走 cwd」——两个 builder 签名一致，差别藏在实现里。
    """
    return _deepseek_base_argv(effort) + ["--session-id", session_id, brief]


def build_deepseek_resume_argv(dir_abs, effort, session_id, brief):
    return _deepseek_base_argv(effort) + ["--resume", session_id, brief]


def _last_result_event(round_text):
    """本轮 JSONL 里最后一条 `result` 事件；没有就 `None`。

    **逐行 try/except，坏行跳过。** 实测进程被 SIGINT 杀掉时最后一行必然是残的，
    `status` 读活日志时同样会撞到——一个残行让整条判据抛异常是不可接受的。
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

    这是 `judge` 一行不改的全部原因：现有那条「报告没出现＝没正常收尾」原样生效。
    把结构化信号用在这里、而不是给 judge 加分支，判据就仍然只有一个家。
    """
    event = _last_result_event(round_text)
    if event is None or event.get("subtype") != "success":
        return
    report_path.write_text(event.get("result") or "")


def deepseek_log_lines(round_text):
    """要补进日志的行；一切正常就是空列表。

    `judge` 的 `detail` 来自 `runtime_error_lines`，认的是 `^ERROR:` 之类。
    JSONL 每行以 `{` 开头，**一条都不会命中**——不补的话失败时 `detail` 是空的。
    补一行现有分类器原样认得的，比给 judge 加分支好：判据还是一个家。

    两种要补：
      非 success        → `failed`（报告没写）
      工具调用被拒      → 报告在 + 有错误行 = `suspect`，退 3「要人看一眼」。
                          **这是「零工作量的成功」的第二道防线**：第一道是
                          `--dangerously-skip-permissions`，但它万一失效，
                          这里让它变红而不是静默判成功。
    """
    event = _last_result_event(round_text)
    if event is None:
        return []
    lines = []
    if event.get("subtype") != "success":
        head = (event.get("result") or "").strip().splitlines()
        lines.append(f"ERROR: {event.get('subtype')}: {head[0] if head else '没有更多信息'}")
    denied = event.get("permission_denials") or []
    if denied:
        names = "、".join(sorted({d.get("tool_name", "?") for d in denied}))
        lines.append(f"ERROR: 本轮有 {len(denied)} 次工具调用被拒（{names}）"
                     f"——子代理可能什么都没做成，报告不可尽信")
    return lines


def agent_comm(runner):
    """这个 runner 的进程在 `/proc/<pid>/comm` 里叫什么。

    实测：`claude-deepseek` 是 `exec` 掉的 bash 包装，所以真实进程的 comm 是
    `claude`，没有多一层——`Popen.pid` 就是它。
    """
    return b"codex" if runner == CODEX else b"claude"
```

`find_codex_pid` 改名 `find_agent_pid(runner, needle)`：`comm` 走 `agent_comm(runner)`，
needle 走参数（codex 传报告路径、deepseek 传 uuid）。
**「按 argv 元素精确比对、绝不用正则」这条原样保留**，它的那段注释一个字不改。

- [ ] **Step 4: 跑全量**

- [ ] **Step 5: 提交**

---

### Task 4: tee 的行模式 + `thinking_tokens` 过滤

**背景（实测）：** `thinking_tokens` 每 token 一条——8 秒 2320 条、462 KB（约 58 KB/s）。
本次 spec 审查那**一个**子代理的日志跑到了 **12 MB**。没有开关能关掉它。

- [ ] **Step 1: 写失败的测试**

```python
class TestTeeLineMode(_HomeSandbox):
    def test_codex那条路径仍然不过滤(self):
        # **护栏。** codex 的 tee 注释说明了分块读是承重的：banner 只有 ~170 字节，
        # 之后可能思考几十分钟，按行读会把 session id 卡在缓冲里，
        # 包装进程此时被杀这一轮就再也 resume 不回来。
        self.assertIsNone(ca.log_line_filter(ca.CODEX))

    def test_deepseek滤掉thinking_tokens(self):
        keep = ca.log_line_filter(ca.DEEPSEEK)
        self.assertFalse(keep('{"type":"system","subtype":"thinking_tokens","n":1}'))
        for good in ('{"type":"result","subtype":"success"}', '{"type":"assistant"}',
                     '{"type":"system","subtype":"init"}'):
            self.assertTrue(keep(good))

    def test_坏行要留着(self):
        # 残行是「这一轮被杀在半路」的现场证据。滤掉它等于把事实也抹掉。
        self.assertTrue(ca.log_line_filter(ca.DEEPSEEK)('{"type":"system","subty'))

    def test_行模式攒半行而不是切坏(self):
        self.assertEqual(ca.split_complete_lines([b'{"a":1}\n{"b', b'":2}\n']),
                         ['{"a":1}', '{"b":2}'])
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

```python
DEEPSEEK_LOG_NOTE = ("===== 本轮日志已滤掉 claude 的 thinking_tokens 进度事件"
                     "（每 token 一条，实测约 58 KB/s）。其余事件一字未改。 =====\n")


def log_line_filter(runner):
    """这个 runner 的日志行过滤器；`None` 表示不过滤、走原来的分块读。

    **codex 侧必须是 `None`**（见 `_tee_until_exit` 那段注释，分块读是承重的）。
    deepseek 侧滤掉 `thinking_tokens`：不滤的话 30 分钟的任务约 104 MB，而判据要扫日志。
    **坏行要留着**——它是现场证据。
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
            return True
        return not (event.get("type") == "system"
                    and event.get("subtype") == "thinking_tokens")
    return keep
```

`_tee_until_exit` 加行模式分支，**codex 那一支一个字节都不动**。

- [ ] **Step 4: 跑全量 + 拿真实现场日志验一次**

```bash
python3 -m unittest test_codex_sub_agent 2>&1 | tail -3
python3 -c "
import codex_sub_agent as ca, pathlib, glob
keep = ca.log_line_filter(ca.DEEPSEEK)
f = sorted(glob.glob('/home/xy/.claude/jobs/*/tmp/ds-review/out.jsonl'))
if not f: print('没有现场日志，跳过'); raise SystemExit
raw = pathlib.Path(f[-1]).read_text(errors='replace').splitlines()
kept = [l for l in raw if keep(l)]
print(f'原始 {len(raw)} 行 / {sum(map(len, raw))} 字节 → 滤后 {len(kept)} 行 / {sum(map(len, kept))} 字节')
print(f'压掉 {100 - 100*len(kept)/len(raw):.1f}%')
"
```
预期压掉 95% 以上。**用的是真实现场日志，不是 fixture**——模块头清单第 8 条说的就是
fixture 经过了我们的手。

- [ ] **Step 5: 提交**

---

### Task 5: CLI —— `--runner`、三条硬拒绝、以及它们的闸序

- [ ] **Step 1: 写失败的测试**

```python
class TestRunnerCLI(_HomeSandbox):
    def test_runner必填(self):
        with self.assertRaises(SystemExit):
            ca.build_parser().parse_args(["run", "--task", "t", "--dir", "/tmp",
                                          "--brief", "/tmp/b.md", "--effort", "low",
                                          "--account", "default", "--no-skill"])

    def test_deepseek给account当场拒(self):
        with self.assertRaises(ca.Rejected) as got:
            ca.cmd_run(self._args(runner="deepseek", account="acct2", effort="max"))
        self.assertIn("账号", got.exception.message)

    def test_deepseek的effort不是max就拒(self):
        # 不静默改成 max——claude-deepseek 自己的注释：「静默跑错模型变成大声退 2」
        with self.assertRaises(ca.Rejected) as got:
            ca.cmd_run(self._args(runner="deepseek", account=None, effort="high"))
        self.assertIn("max", got.exception.message)

    def test_codex仍然必须给account(self):
        with self.assertRaises(ca.Rejected):
            ca.cmd_run(self._args(runner="codex", account=None, effort="low"))

    def test_三条拒绝排在任何状态变更之前(self):
        # **承重。** cmd_run 里有一段会删旧元数据的迁移分支。校验排在它后面的话，
        # 一次被拒的调用已经把一个任务从 status/resume/stop 里抹掉了。
        # 代码里已有同款教条（check_can_resume：凡是对已交进来输入的纯检查，
        # 一律排在不可逆动作之前）。
        home = ca.ensure_isolation(ca.CODEX, "acct2")
        ca.write_meta(home, "t", ca.new_meta("t", ca.CODEX, "acct2", "/tmp", "low", ()))
        with self.assertRaises(ca.Rejected):
            ca.cmd_run(self._args(task="t", runner="deepseek", account=None, effort="high"))
        self.assertTrue(ca.meta_path(home, "t").exists(), "被拒的调用不许动任何状态")

    def test_resume和stop都不收runner(self):
        for cmd in ("resume", "stop"):
            with self.assertRaises(SystemExit):
                ca.build_parser().parse_args([cmd, "t", "--runner", "codex"])
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 改 parser 与 `cmd_run`**

```python
    r.add_argument("--runner", required=True, choices=list(RUNNERS), help="派给哪个 agent 跑")
```

`--account` 改成可选；按 runner 的校验写在 `cmd_run` **最前面**——argparse 表达不了
「A 必填当且仅当 B 是某值」，而写成文档约定正是仓库规范第 5 条要消灭的东西。

`run_codex` 的 `Popen` 加 `cwd=`（codex 侧传 workdir 也无害，它本来就有 `--cd`；
**两侧都传，少一个分支**）。

- [ ] **Step 4: 跑全量**

- [ ] **Step 5: 真跑一次冒烟——必须产出真文件**

```bash
cd /home/xy/.claude/skills/codex-sub-agent
WD=$(mktemp -d)
printf '在当前工作目录创建 proof.txt，内容写 SMOKE-OK。然后回答：完成\n' > /tmp/smoke-ds.md
./codex_sub_agent.py run --task smoke-deepseek --runner deepseek --dir "$WD" \
  --brief /tmp/smoke-ds.md --effort max --no-skill
echo "退出码 $?"
cat "$WD/proof.txt"        # ← **必须是 SMOKE-OK**
./codex_sub_agent.py status smoke-deepseek
```
**只断言「报告非空」挡不住「零工作量的成功」**——必须核对文件真的落在 `--dir` 里。
**codex 侧不跑冒烟**：三个账号全部限流（acct3 到 9/25 17:04、default 到 9/26 17:47、
acct2 到 9/28 05:53）。

- [ ] **Step 6: 提交**

---

### Task 6: 改名——盘上契约一个都不许跟着改

**Files:** 目录、两个 `.py`、`SKILL.md`、`README.md`、`~/.claude/settings.json`

> **v1 的这一步写了 `sed -i 's/codex-sub-agent/sub-agent-runner/g'`。那是个 bug。**
> `ROUND_MARK` 和 `INTERRUPT_MARK` 的**文本里含 `codex-sub-agent`**，而它们写在每一份
> 日志文件里、被 `_ROUND_LINE` / `_MARK_LINE` 整行匹配。一次全局 `sed` 会让所有现存日志的
> 轮次边界和打断痕迹当场认不出来：`read_last_round` 把整份日志当成这一轮（前几轮的错误
> 全算进来），被打断的轮次从 `interrupted` 退回 `failed`（代码里记着那个 28k token 的坑）。

- [ ] **Step 1: 先把盘上契约钉住**

```python
    def test_盘上契约的字面量一个都没改(self):
        """这三样写在盘上，改了就读不回来。命令名改，它们不改。"""
        self.assertEqual(ca.ROUND_MARK, "===== codex-sub-agent ")
        self.assertEqual(ca.INTERRUPT_MARK,
                         "----- codex-sub-agent 本轮被 INT 打断，上下文保留，可 resume -----")
        self.assertEqual(ca.isolation_home(ca.CODEX, "default").name, ".codex-subagent")
```

先加这条测试并确认它**绿**（改名前就该绿），它就是改名那一步的安全网。

- [ ] **Step 2: 查清楚所有引用，逐处判断是「名字」还是「契约」**

```bash
cd /home/xy/.claude/skills/codex-sub-agent
grep -n "codex-sub-agent\|codex_sub_agent" codex_sub_agent.py test_codex_sub_agent.py \
  SKILL.md README.md ~/.claude/settings.json 2>/dev/null
```
**逐行看。** 凡是 `ROUND_MARK` / `INTERRUPT_MARK` 的定义、以及测试里拿它们当样本的地方
（含 `f"codex-sub-agent {cmd}"` 那条），都**不改**。

- [ ] **Step 3: 改文件名与模块名（不碰契约）**

```bash
git mv codex_sub_agent.py sub_agent_runner.py
git mv test_codex_sub_agent.py test_sub_agent_runner.py
sed -i 's/codex_sub_agent/sub_agent_runner/g' sub_agent_runner.py test_sub_agent_runner.py
python3 -m unittest test_sub_agent_runner 2>&1 | tail -3
```
`codex_sub_agent`（下划线）是模块名，**不出现在任何盘上契约里**，可以全局替换。
`codex-sub-agent`（连字符）**不可以**——它在两个 MARK 里。

- [ ] **Step 4: 逐处改连字符那个，命令名改、契约不改**

改：`SKILL.md` / `README.md` 里的命令名、`argparse` 的 `prog=`、帮助文本。
**不改**：`ROUND_MARK`、`INTERRUPT_MARK` 及其测试样本。

```bash
python3 -m unittest test_sub_agent_runner -k 盘上契约 -v   # 必须仍然绿
python3 -m unittest test_sub_agent_runner 2>&1 | tail -3
```

- [ ] **Step 5: 提交，然后移动目录**

```bash
git commit -am "refactor: 改名 codex-sub-agent → sub-agent-runner（盘上契约不动）"
cd .. && mv codex-sub-agent sub-agent-runner
```

- [ ] **Step 6: 更新外部引用并核对**

```bash
grep -rn "codex-sub-agent" ~/.claude/settings.json ~/.claude/CLAUDE.md 2>/dev/null
ls -d ~/.codex-subagent* ~/.claude-subagent
cd ~/.claude/skills/sub-agent-runner && python3 -c "
import sub_agent_runner as ca
print('元数据仍然读得出:', len(ca.all_metas()))
print('ROUND_MARK:', repr(ca.ROUND_MARK))"
```
预期：`~/.codex-subagent*` 原样在；元数据数量和 Task 1 量到的一致；`ROUND_MARK` 未变。

- [ ] **Step 7: 文档**

`SKILL.md` 的账号那一格拆成 runner + 账号；写明 deepseek 的 effort 只能是 max
（**说事实，不写「记得传 max」**——代码已经硬拒绝了）。README 同步。

---

## Self-Review 结果

| spec 判据 | Task |
|---|---|
| 1 改名 + 盘上契约一个没改 | Task 6（Step 1 的测试是安全网，Step 6 核对） |
| 2 三条硬拒绝 + 排在状态变更之前 | Task 5（`test_三条拒绝排在任何状态变更之前`） |
| 3 真跑要产出真文件 | Task 5 Step 5 |
| 4 权限受阻要变红 | Task 3 的 `test_工具被拒时也补一行_于是judge给suspect` |
| 5 工作目录 | Task 3（argv 里没有）+ Task 5（`Popen(cwd=)`）+ Step 5 冒烟核对 |
| 6 判据 success/打断 | Task 3 |
| 7 日志过滤、codex 未改、坏行不崩 | Task 4 |
| 8 uuid 是独立 argv 元素 | Task 3 第一条测试 |
| 9 PID 反查 | Task 3 的 `agent_comm` + `find_agent_pid` |
| 10 元数据 + 260 份迁移 | Task 1 + Task 2 |
| 11 deepseek 任务查得到 | Task 2 的 `isolation_roots` + `test_deepseek的任务status查得到` |
| 12 测试护栏 + codex 行为未变 | Global Constraints + Task 4 的护栏测试 |
| 13 收尾 | 工作流程第 8 步，本计划之外 |

**无遗漏。**

**占位符扫描：** 无 TBD/TODO；每个代码步骤都有可粘贴的真代码。

**类型一致性：** `isolation_home(runner, account)`、`new_meta(task, runner, account, …)`、
`isolation_roots() -> list[Path]`、`finish_deepseek_round(round_text, report_path)`、
`deepseek_log_lines(round_text) -> list[str]`、`log_line_filter(runner) -> callable|None`
——各 Task 定义与调用一致。后两个收同一种输入（本轮日志文本），这正是上一轮那个
「判撞没撞上用错误行、判几点恢复用整轮文本」的 bug 的反面。
