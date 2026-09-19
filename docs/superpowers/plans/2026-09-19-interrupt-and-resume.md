# `interrupt-and-resume` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `codex-agent` 加第五条子命令 `interrupt-and-resume`，把「打断 + 确认退出 + 续跑」焊成一个不可分的动作；同一改动里修掉 `stop` 不留打断痕迹、以及被打断的轮次退出码说谎这两个已实测的 bug。

**Architecture:** 三个新纯函数（`interrupt_codex`、`wait_for_exit`、`_resume_with`）+ 一个新命令；`_STATES` 加第五态。现有 `cmd_stop` 与 `forward_as_sigint` 改为走同一个 `interrupt_codex`，让「发信号」和「留痕」不可能再分家。

**Tech Stack:** Python 3 stdlib（新增 `import time`）。

## Global Constraints

- 对照 spec：`docs/superpowers/specs/2026-09-19-interrupt-and-resume-design.md`，每个决定以它为准。
- **意图基准**：用户的原始文档 `/home/xy/.claude/jobs/d09f8216/tmp/intent/ORIGINAL-codex-sub-agent-SKILL.md`。拿不准某条约束该不该保时，回去读它。
- 零第三方依赖；**import 一律并到文件头**。
- **不设任何默认缺省值**：新函数的 timeout / poll_interval 由调用方显式传常量，函数签名里不写默认值。
- 测试命令 `python3 -m unittest test_codex_agent -v`，现有 116 个必须全绿。
- **空测试比没测试更糟**：凡依赖外部进程／文件的测试，先断言前提成立。这个仓库在这上面栽过四次。
- 每个任务结束提交一次，提交信息中文、说清楚为什么。
- 知识写进代码注释，贴在防住它的那行旁边，不另开文档。

---

### Task 1: 第五态 `interrupted`

**Files:** Modify `codex_agent.py` · Test `test_codex_agent.py`

**Interfaces:**
- Consumes: 现有 `_STATES` / `EXIT` / `_SEVERITY` / `judge`
- Produces: `EXIT["interrupted"] == 5`；`judge` 在认出 `INTERRUPT_MARK` 时返回 state `"interrupted"`

- [ ] **Step 1: 写失败的测试**

```python
class TestInterruptedIsItsOwnState(unittest.TestCase):
    """被打断的轮次该做的事是 resume，和其余四态都不同，所以它是第五个状态。

    只修日志标记是不够的：judge 以前返回 Verdict("failed", "本轮被 INT 打断…")，
    文字对了而**退出码仍是 1**，而 SKILL.md 把退出码印成了对外契约——
    agent 照它做决定，拿到 failed 会从头重跑，把保着的上下文和 token 一起扔掉。
    """

    def test_五个状态的退出码逐个钉死(self):
        # 绝对值断言。写成 EXIT["x"] == EXIT["x"] 那种自指是空测试，本仓栽过。
        self.assertEqual(ca.EXIT, {"success": 0, "failed": 1, "suspect": 3,
                                   "running": 4, "interrupted": 5})

    def test_护栏拒绝的码不与任何判据结论相撞(self):
        self.assertNotIn(ca.USAGE_ERROR, set(ca.EXIT.values()))

    def test_严重度清单恰好覆盖五态且顺序正确(self):
        self.assertEqual(ca._SEVERITY,
                         ["success", "interrupted", "running", "suspect", "failed"])

    def test_有打断标记且无报告时状态是interrupted而不是failed(self):
        d = pathlib.Path(tempfile.mkdtemp())
        report, log = d / "t.md", d / "t.log"
        log.write_text(ca.round_separator("run", "t", "2026-09-19T00:00:00") + "\n"
                       + ca.INTERRUPT_MARK + "\n")
        v = ca.judge(report, log, None)
        self.assertEqual(v.state, "interrupted")
        self.assertEqual(ca.EXIT[v.state], 5)
        self.assertIn("resume", v.reason)

    def test_额度上限和写锁仍然是failed_它们resume救不回来(self):
        # 这两条的补救是换账号/新起任务，不是 resume——不能被第五态顺手吃掉
        d = pathlib.Path(tempfile.mkdtemp())
        for mark in (ca.USAGE_LIMIT_MARK, ca.THREAD_LOCK_MARK):
            with self.subTest(mark=mark):
                report, log = d / "t.md", d / "t.log"
                log.write_text(ca.round_separator("run", "t", "2026-09-19T00:00:00")
                               + "\n" + mark + "\n")
                self.assertEqual(ca.judge(report, log, None).state, "failed")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestInterruptedIsItsOwnState -v`
Expected: FAIL，`EXIT` 里没有 `interrupted`

- [ ] **Step 3: 最小实现**

```python
# 五个状态回答的是同一个问题：**接下来该干什么**。
#   success 不用干什么 / suspect 去看一眼 / failed 查原因并重跑 /
#   running 等 / interrupted **接着 resume**
# interrupted 的处置和其余四个都不同，所以它是一个状态，不是 failed 下的一条理由。
# 顺序即严重度（越靠后越该拦住调用方）：被打断的任务是等着你续跑，
# 比「还在跑」更不该拦人，但比 success 更需要你做点什么。
# 码值与顺序无关，2 永久留给护栏拒绝（USAGE_ERROR）。
_STATES = (("success", 0), ("interrupted", 5), ("running", 4), ("suspect", 3), ("failed", 1))
```

`judge` 里那一行改成：

```python
        if INTERRUPT_MARK in round_text:
            return Verdict("interrupted",
                           "本轮被 INT 打断，上下文保留——接着 resume 即可，不用重跑", errors)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，121 个全绿（原 116 + 新 5）

- [ ] **Step 5: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "fix: 被打断的轮次是第五态 interrupted，不是 failed——退出码也在说谎"
```

---

### Task 2: `interrupt_codex()` —— 把发信号和留痕焊成一个动作

**Files:** Modify `codex_agent.py` · Test `test_codex_agent.py`

**Interfaces:**
- Consumes: `note_interrupt`、`INTERRUPT_MARK`
- Produces: `interrupt_codex(pid: int, log_path: pathlib.Path) -> bool`（信号是否送出）
- 三个调用点全部改走它：`forward_as_sigint`、`cmd_stop`、（Task 4 的新命令）

- [ ] **Step 1: 写失败的测试**

```python
class TestInterruptCodex(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.log = self.d / "t.log"
        self.log.write_text("")

    def test_发出信号的同时一定留痕(self):
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import signal,sys,time\n"
                                 "signal.signal(signal.SIGINT, lambda s,f: sys.exit(0))\n"
                                 "time.sleep(30)"])
        try:
            self.assertTrue(ca.pid_alive(proc.pid), "前提不成立：陪练进程没起来")
            self.assertTrue(ca.interrupt_codex(proc.pid, self.log))
            self.assertIn(ca.INTERRUPT_MARK, self.log.read_text())
            proc.wait(timeout=5)
            self.assertEqual(proc.returncode, 0, "它该是被 INT 正常收走的")
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()

    def test_进程已经不在时不留假痕迹(self):
        # 信号没送出去就不该说「它被打断了」
        self.assertFalse(ca.interrupt_codex(2 ** 22, self.log))
        self.assertEqual(self.log.read_text(), "")

    def test_只发INT绝不发TERM(self):
        # SIGTERM 会让 thread 永久锁死，之后 resume 永远报 thread-store conflict
        with mock.patch.object(ca.os, "kill") as k:
            ca.interrupt_codex(4242, self.log)
        k.assert_called_once_with(4242, signal.SIGINT)


class TestEveryInterruptPathLeavesAMark(unittest.TestCase):
    """三条打断路径必须都留痕。漏一条就会出现「stop 说可 resume、status 说 failed」。"""

    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.p = mock.patch.object(ca.pathlib.Path, "home", staticmethod(lambda: self.home))
        self.p.start()
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").write_text("{}")

    def tearDown(self):
        self.env.stop(); self.p.stop()

    def test_stop这条路也留痕(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", ca.new_meta("t", "default", "/tmp", "low"))
        log = ca._log_path(d, "t")
        log.write_text("")
        args = ca.build_parser().parse_args(["stop", "t"])
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca.os, "kill") as k:
            ca.cmd_stop(args)
        k.assert_called_once_with(4242, signal.SIGINT)
        self.assertIn(ca.INTERRUPT_MARK, log.read_text(),
                      "stop 只发信号不留痕 → status 会把被打断的轮次报成 failed")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestInterruptCodex test_codex_agent.TestEveryInterruptPathLeavesAMark -v`
Expected: FAIL，`module 'codex_agent' has no attribute 'interrupt_codex'`

- [ ] **Step 3: 最小实现**

```python
def interrupt_codex(pid, log_path):
    """发 SIGINT 并在日志留痕。这两件事必须一起发生，所以焊在一个函数里。

    拆开放就会漏，而且**已经漏过一次**：2026-09-19 实测，`cmd_stop` 只发信号没留痕，
    于是它刚打印完「上下文保留，可 resume」，同一个任务的 `status` 就说
    `failed —— 报告缺失或为空＝没正常收尾`、退出码 1，harness 的完成通知跟着说 failed。
    三者当场互相矛盾——而 agent 看到 failed 会从头重跑，把保着的上下文和 token 一起扔掉。

    只发 INT，永不 TERM：SIGTERM 会让 thread 永久锁死，之后 resume 永远报
    thread-store conflict，等多久都不释放，上下文全丢。

    先发信号、成功了再留痕：信号没送出去（进程已退出）就不该留下假痕迹。
    返回信号是否送出，让调用方能分辨「我打断了它」和「它本来就没在跑」。
    """
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        return False
    note_interrupt(log_path)
    return True
```

`forward_as_sigint` 里那两行换成一行：

```python
        def forward_as_sigint(signum, frame):
            # 无论包装器被谁、用什么信号停，codex 收到的永远是 INT，上下文永远可 resume。
            # 刻意不在这里退出：让 tee 循环自然跑完，判据照样出、完成通知照样带结论。
            interrupt_codex(proc.pid, _log_path(home, task))
```

`cmd_stop` 里那一行换成：

```python
    if not interrupt_codex(pid, _log_path(home, args.task)):
        print(f"任务 {args.task} 刚好已经结束了")
        return EXIT["success"]
    print(f"已向 {args.task} (pid={pid}) 发 SIGINT，上下文保留，可 resume")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，125 个全绿

- [ ] **Step 5: 突变复验**

把 `cmd_stop` 改回裸 `os.kill(pid, signal.SIGINT)`（不留痕），跑测试。
Expected: `test_stop这条路也留痕` **必须红**。确认后还原。

- [ ] **Step 6: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "fix: 发 INT 与留痕焊成 interrupt_codex，三条路都走它——stop 那条漏了"
```

---

### Task 3: `wait_for_exit()` —— 只轮询，不加火力

**Files:** Modify `codex_agent.py` · Test `test_codex_agent.py`

**Interfaces:**
- Consumes: `find_codex_pid`
- Produces: `wait_for_exit(report_path: str, timeout: float, poll_interval: float) -> bool`
- 常量 `INTERRUPT_EXIT_TIMEOUT = 60`、`INTERRUPT_POLL_INTERVAL = 0.2`

- [ ] **Step 1: 写失败的测试**

```python
class TestWaitForExit(unittest.TestCase):
    def test_进程退出后返回True(self):
        calls = [4242, 4242, None]
        with mock.patch.object(ca, "find_codex_pid", side_effect=calls):
            self.assertTrue(ca.wait_for_exit("/x/reports/t.md", 5, 0.01))

    def test_一直不退则超时返回False(self):
        with mock.patch.object(ca, "find_codex_pid", return_value=4242):
            self.assertFalse(ca.wait_for_exit("/x/reports/t.md", 0.05, 0.01))

    def test_等待期间绝不发任何信号(self):
        # 超时的正确处置是告诉调用方稍后再来，不是加大火力。
        # 升级到 SIGTERM 会让会话永久锁死，而那是不可逆的。
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca.os, "kill") as k:
            ca.wait_for_exit("/x/reports/t.md", 0.05, 0.01)
        k.assert_not_called()

    def test_超时是上界不是等待时长(self):
        # 一确认退出就立刻往下走，不把 timeout 睡满
        started = time.monotonic()
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            self.assertTrue(ca.wait_for_exit("/x/reports/t.md", 30, 0.01))
        self.assertLess(time.monotonic() - started, 1.0)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestWaitForExit -v`
Expected: FAIL，`no attribute 'wait_for_exit'`

- [ ] **Step 3: 最小实现**

```python
# 打断之后最多等它收尾这么久。**是上界不是等待时长**——一确认退出就立刻往下走。
# 60 秒的依据：codex 收到 INT 后要把 rollout 落盘才退出，实测是秒级；
# 给到 60 秒是留足余量，同时保证卡住时调用方不会被无限期挂着。
INTERRUPT_EXIT_TIMEOUT = 60
INTERRUPT_POLL_INTERVAL = 0.2


def wait_for_exit(report_path, timeout, poll_interval):
    """轮询真实 PID，直到它真的退出。返回是否在 timeout 之内退出。

    这一步是 `interrupt-and-resume` 存在的头号理由。用户的原始文档记过：
    对**还在跑**的会话 resume，报的错和 SIGTERM 永久锁死一模一样
    （thread-store conflict），而两者处置完全相反——一个该等，一个该弃；
    2026-09-17 两次 resume 都栽在这里。手工连着敲 stop 和 resume，
    几乎必然撞进「信号已发出、进程还没退」这个窗口。

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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，129 个全绿

- [ ] **Step 5: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: wait_for_exit 只轮询不加火力——超时也绝不升级到 TERM"
```

---

### Task 4: 抽出 `_resume_with()` 并实现 `cmd_interrupt_and_resume()`

**Files:** Modify `codex_agent.py` · Test `test_codex_agent.py`

**Interfaces:**
- Consumes: Task 2/3 的产出、现有 `find_meta`、`ensure_isolation`、`run_codex`、`judge`
- Produces:
  - `_resume_with(kind: str, home, meta: dict, task: str, brief_path: str, effort: str) -> int`
  - `cmd_interrupt_and_resume(args) -> int`

- [ ] **Step 1: 写失败的测试**

```python
class TestInterruptAndResume(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.p = mock.patch.object(ca.pathlib.Path, "home", staticmethod(lambda: self.home))
        self.p.start()
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").write_text("{}")
        self.workdir = self.home / "repo"; self.workdir.mkdir()
        self.brief = self.home / "msg.md"; self.brief.write_text("顺便把 X 也改了")
        self.d = ca.ensure_isolation("default")
        meta = ca.new_meta("t", "default", str(self.workdir), "low")
        meta["session_id"] = "sess-1"
        ca.write_meta(self.d, "t", meta)
        ca._log_path(self.d, "t").write_text("")

    def tearDown(self):
        self.env.stop(); self.p.stop()

    def _args(self):
        return ca.build_parser().parse_args(
            ["interrupt-and-resume", "t", "--brief", str(self.brief), "--effort", "low"])

    def test_在跑时先打断再等退出再续跑(self):
        order = []
        with mock.patch.object(ca, "find_codex_pid", side_effect=[4242, None]), \
             mock.patch.object(ca, "interrupt_codex",
                               side_effect=lambda *a: order.append("interrupt") or True), \
             mock.patch.object(ca, "_resume_with",
                               side_effect=lambda *a: order.append("resume") or 0):
            self.assertEqual(ca.cmd_interrupt_and_resume(self._args()), 0)
        self.assertEqual(order, ["interrupt", "resume"], "顺序反了就会撞写锁")

    def test_没在跑时不发信号直接续跑(self):
        # 调用方无法可靠知道自己在哪种情况——查完到动手之间任务可能刚好跑完
        with mock.patch.object(ca, "find_codex_pid", return_value=None), \
             mock.patch.object(ca, "interrupt_codex") as ic, \
             mock.patch.object(ca, "_resume_with", return_value=0):
            self.assertEqual(ca.cmd_interrupt_and_resume(self._args()), 0)
        ic.assert_not_called()

    def test_等不到退出就拒绝续跑且不升级信号(self):
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "interrupt_codex", return_value=True), \
             mock.patch.object(ca, "wait_for_exit", return_value=False), \
             mock.patch.object(ca, "_resume_with") as rw:
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_interrupt_and_resume(self._args())
        rw.assert_not_called()
        self.assertEqual(cm.exception.code, ca.USAGE_ERROR)
        self.assertIn("稍后", cm.exception.message)

    def test_任务不存在就拒绝(self):
        args = ca.build_parser().parse_args(
            ["interrupt-and-resume", "没有", "--brief", str(self.brief), "--effort", "low"])
        with self.assertRaises(ca.Rejected):
            ca.cmd_interrupt_and_resume(args)

    def test_没有session_id就拒绝(self):
        meta = ca.new_meta("t2", "default", str(self.workdir), "low")
        ca.write_meta(self.d, "t2", meta)
        args = ca.build_parser().parse_args(
            ["interrupt-and-resume", "t2", "--brief", str(self.brief), "--effort", "low"])
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_interrupt_and_resume(args)
        self.assertIn("session id", cm.exception.message)

    def test_工作目录不在了就拒绝(self):
        meta = ca.new_meta("t3", "default", str(self.home / "没了"), "low")
        meta["session_id"] = "sess-3"
        ca.write_meta(self.d, "t3", meta)
        args = ca.build_parser().parse_args(
            ["interrupt-and-resume", "t3", "--brief", str(self.brief), "--effort", "low"])
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_interrupt_and_resume(args)
        self.assertIn("工作目录", cm.exception.message)

    def test_日志分隔符写的是本命令而不是resume(self):
        # 日志要看得出这一轮是被插话打断后续上的
        seen = {}
        with mock.patch.object(ca, "find_codex_pid", return_value=None), \
             mock.patch.object(ca, "run_codex",
                               side_effect=lambda kind, *a, **k: seen.update(kind=kind)), \
             mock.patch.object(ca, "judge",
                               return_value=ca.Verdict("success", "ok", [])):
            ca.cmd_interrupt_and_resume(self._args())
        self.assertEqual(seen["kind"], "interrupt-and-resume")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestInterruptAndResume -v`
Expected: FAIL，parser 不认 `interrupt-and-resume`

- [ ] **Step 3: 最小实现**

先把 `cmd_resume` 的后半段抽出来（两条路共用）：

```python
def _resume_with(kind, home, meta, task, brief_path, effort):
    """两条路共用的续跑动作：`resume` 和 `interrupt-and-resume`。

    它只管「已经确定停了之后怎么续」，**不判断该不该停**——
    `cmd_resume` 在调它之前拒绝还在跑的任务，
    `cmd_interrupt_and_resume` 在调它之前把它打断并确认退出。
    这条边界是刻意的：谁来停、停不停，是调用方的判断（用户的原始文档：
    「要不要为此打断它，看信息本身」）；怎么续，是工具的事。

    kind 进日志分隔符，所以日志里看得出这一轮是被插话打断后续上的。
    """
    if not meta["session_id"]:
        reject(f"任务 {task} 没有记到 session id，无法 resume，只能新起一个任务")
    workdir = pathlib.Path(meta["dir"])
    if not workdir.is_dir():
        reject(f"任务 {task} 的工作目录 {workdir} 不在了（worktree 被删？）。"
               f"codex 会以 os error 2 当场崩，所以这里直接拒。")
    brief_file = pathlib.Path(brief_path).expanduser()
    if not brief_file.is_file():
        reject(f"--brief {brief_path} 不是文件")

    ensure_isolation(meta["account"])
    brief = prepend_skill_guard(brief_file.read_text())
    # 元数据描述的是**最后一次调用**：effort 和开跑时间都刷新。
    # 完整的轮次历史不在这里，在日志的分隔符里（每轮一行，带时间戳）。
    meta["effort"] = effort
    meta["started_at"] = _now_iso()
    run_codex(kind, home, task, meta,
              lambda r: build_resume_argv(meta["dir"], meta["session_id"], effort, r, brief))
    verdict = judge(_report_path(home, task), _log_path(home, task), None)
    _print_verdict(task, verdict)
    return EXIT[verdict.state]
```

`cmd_resume` 收缩成守闸 + 调用：

```python
def cmd_resume(args):
    home, meta = find_meta(args.task)
    if meta is None:
        reject(f"没有这个任务：{args.task}")
    # resume 之前必须确认真的退出了：对还在跑的会话 resume，报的错和 SIGTERM 锁死
    # 一模一样（thread-store conflict），而处置完全相反——一个该等，一个该弃。
    # 本命令刻意**不替调用方打断**：要打断请用 interrupt-and-resume，那个名字
    # 把代价写在脸上。
    if find_codex_pid(str(_report_path(home, args.task))) is not None:
        reject(f"任务 {args.task} 还在跑，resume 会撞上它自己的写锁。"
               f"等它结束，或用 `codex-agent interrupt-and-resume {args.task}`。")
    return _resume_with("resume", home, meta, args.task, args.brief, args.effort)
```

新命令：

```python
def cmd_interrupt_and_resume(args):
    """打断当前轮 + 确认它真的退出了 + 用新消息续跑，三件事不可分。

    为什么要有这条命令，而不是让调用方自己连着敲 stop 和 resume：
    `stop` 只是把 SIGINT 发出去就返回，codex 要过一会儿才真的退出。
    手工连敲几乎必然撞进那个窗口，而撞上之后的报错和「SIGTERM 永久锁死」
    一模一样——两种病因处置完全相反（一个该等、一个该弃），撞上了还判不出来。
    把「确认退出」焊在中间，这个窗口就不存在了。

    **要不要为此打断，仍然是调用方的判断**（用户的原始文档：「要不要为此打断它，
    看信息本身」）。命令名把代价写在脸上，工具不替谁做这个决定。
    """
    home, meta = find_meta(args.task)
    if meta is None:
        reject(f"没有这个任务：{args.task}")
    report = _report_path(home, args.task)
    pid = find_codex_pid(str(report))
    if pid is None:
        # 三种入场情况都要吃：调用方无法可靠知道自己在哪一种——
        # 查完到动手之间，任务可能刚好跑完。所以两种都走通，并如实说走了哪条。
        print(f"[codex-agent] {args.task} 本来就没在跑，直接续跑")
    else:
        interrupt_codex(pid, _log_path(home, args.task))
        print(f"[codex-agent] {args.task} 还在跑（pid={pid}），已发 SIGINT 并在日志留痕")
        if not wait_for_exit(str(report), INTERRUPT_EXIT_TIMEOUT, INTERRUPT_POLL_INTERVAL):
            reject(f"任务 {args.task} 收到 INT 后 {INTERRUPT_EXIT_TIMEOUT} 秒还没退出，"
                   f"还在收尾。稍后重试——绝不升级信号：SIGTERM 会让会话永久锁死，不可逆。")
        print("[codex-agent] 已确认退出，本轮被提前结束——已做的部分留在上下文里")
    return _resume_with("interrupt-and-resume", home, meta, args.task, args.brief, args.effort)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，136 个全绿

- [ ] **Step 5: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: interrupt-and-resume——把「确认退出」焊在打断与续跑之间"
```

---

### Task 5: CLI 接线与参数一致性

**Files:** Modify `codex_agent.py` · Test `test_codex_agent.py`

**Interfaces:**
- Consumes: `build_parser`、`task_name`、`EFFORTS`
- Produces: 子命令 `interrupt-and-resume`

- [ ] **Step 1: 写失败的测试**

```python
class TestInterruptAndResumeParser(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.p = mock.patch.object(ca.pathlib.Path, "home", staticmethod(lambda: self.home))
        self.p.start()
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.p.stop()

    def test_与resume同一张参数表(self):
        parser = ca.build_parser()
        for missing in ["--brief", "--effort"]:
            argv = ["interrupt-and-resume", "t", "--brief", "b.md", "--effort", "low"]
            i = argv.index(missing); del argv[i:i + 2]
            with self.subTest(missing=missing), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_effort五档正反都验(self):
        parser = ca.build_parser()
        for e in ca.EFFORTS:
            parser.parse_args(["interrupt-and-resume", "t", "--brief", "b.md", "--effort", e])
        with self.assertRaises(SystemExit):
            parser.parse_args(["interrupt-and-resume", "t", "--brief", "b.md", "--effort", "中"])

    def test_任务名字符集同样把关(self):
        parser = ca.build_parser()
        for bad in ["../escape", "a/b", "a|b", "fix(api)"]:
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parser.parse_args(["interrupt-and-resume", bad,
                                   "--brief", "b.md", "--effort", "low"])

    def test_不收account_账号是查出来的(self):
        with self.assertRaises(SystemExit):
            ca.build_parser().parse_args(["interrupt-and-resume", "t", "--brief", "b.md",
                                          "--effort", "low", "--account", "default"])

    def test_不提供会造成误用的旋钮(self):
        # 没有第二种正确行为，每个旋钮都是一个让调用方做错的机会
        parser = ca.build_parser()
        for bad in ["--now", "--wait", "--force", "--timeout", "--message"]:
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parser.parse_args(["interrupt-and-resume", "t", "--brief", "b.md",
                                   "--effort", "low", bad, "x"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestInterruptAndResumeParser -v`
Expected: FAIL，`invalid choice: 'interrupt-and-resume'`

- [ ] **Step 3: 最小实现**

在 `build_parser` 里 `resume` 之后加：

```python
    # 名字刻意长而直白：它会**截断当前轮**，这个代价必须写在脸上。
    # 仓库规范第 2 条说「不清晰的单词全部换为简单清晰的词组」——
    # `interject` 是个不清晰的单词，`interrupt-and-resume` 是个清晰的词组。
    j = sub.add_parser("interrupt-and-resume",
                       help="打断当前轮并用新消息续跑（会截断当前轮，上下文保留）")
    j.add_argument("task", type=task_name)
    j.add_argument("--brief", required=True)
    j.add_argument("--effort", required=True, choices=EFFORTS)
    j.set_defaults(func=cmd_interrupt_and_resume)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，141 个全绿

- [ ] **Step 5: 自检**

```bash
python3 codex_agent.py --help
python3 codex_agent.py interrupt-and-resume --help
```
Expected: 五条子命令都在；新命令的 help 里看得到「会截断当前轮」

- [ ] **Step 6: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: 接线 interrupt-and-resume，参数表与 resume 逐条一致"
```

---

### Task 6: SKILL.md

**Files:** Modify `SKILL.md`

- [ ] **Step 1: 改三处**

1. 「另外三条命令」→「另外四条命令」，代码块加一行：

```bash
codex-agent interrupt-and-resume <任务名> --brief msg.md --effort low
```

2. 原来那段「`codex exec` 没有收件箱……」后面接上**只有人/模型才能决定的那件事**：

> 所以插话＝**打断当前轮**。`interrupt-and-resume` 把「发信号 → 确认真的退出了 → 续跑」
> 焊成一个动作，你不用自己去踩那个窗口。**要不要为此打断它，看信息本身值不值**——
> 已做的部分留在上下文里不会白做（0.154.0 实测：追问「被打断前你建了哪几个文件」，
> 它自己答得出），但当前这一轮的收尾会没有。

3. 退出码那一行加第五态：

> `0` success、`1` failed、`3` suspect（…）、`4` running、**`5` interrupted（被打断，接着 `resume` 或 `interrupt-and-resume` 即可，不要重跑）**、`2` 参数写错或被护栏拒绝

- [ ] **Step 2: 跑泄漏测试**

Run: `python3 -m unittest test_codex_agent.TestSkillDocDoesNotRepeatCode -v`
Expected: PASS —— 新写的内容里不能出现已由代码保证的约束（信号怎么发、窗口怎么等、痕迹怎么留）

- [ ] **Step 3: 全量测试 + 提交**

```bash
python3 -m unittest test_codex_agent
git add SKILL.md
git commit -m "docs: SKILL.md 加第四条命令与第五态，只写判断力不写机制"
```

---

### Task 7: 端到端真机验证

**Files:** 无（验证性任务）

- [ ] **Step 1: 造一个会跑一阵子的真任务**

```bash
D=/home/xy/.claude/jobs/d09f8216/tmp/iar-smoke; rm -rf $D; mkdir -p $D; cd $D; git init -q
cat > $D/brief.md <<'EOF'
请按顺序做三件事，不要跳过：
1. 创建 s1.txt，内容为一行：s1 done
2. 运行 shell 命令 `sleep 120`（刻意的等待，请真的执行并等它结束）
3. sleep 结束后创建 s2.txt，内容为一行：s2 done
最后一句话说明你创建了哪些文件。
EOF
```

- [ ] **Step 2: 后台起任务**（必须 `run_in_background: true`）

```bash
python3 <仓库绝对路径>/codex_agent.py run --task iar-smoke-2026-09-19 \
  --dir $D --brief $D/brief.md --effort low --account default
```

- [ ] **Step 3: 等它进 sleep 之后插话**

```bash
cat > $D/msg.md <<'EOF'
改一下：不用等 sleep 了，直接创建 s3.txt，内容为一行：interjected ok
并在收尾自述里说明：被打断前你已经成功创建了哪些文件。
EOF
python3 <仓库绝对路径>/codex_agent.py interrupt-and-resume iar-smoke-2026-09-19 \
  --brief $D/msg.md --effort low
```

Expected（逐条记录实际输出）：
- 打印「还在跑（pid=…），已发 SIGINT 并在日志留痕」
- 打印「已确认退出」
- 续跑成功，`s3.txt` 出现
- 收尾自述里 codex **自己说得出被打断前建了 s1.txt** → 证明上下文真的接上了

- [ ] **Step 4: 验证被打断那一轮的结论不再说谎**

```bash
grep -c "本轮被 INT 打断" ~/.codex-subagent/logs/iar-smoke-2026-09-19.log
awk '/interrupt-and-resume/{exit} {print}' ~/.codex-subagent/logs/iar-smoke-2026-09-19.log | tail -5
```
Expected: 打断标记 ≥ 1；**被打断那一轮的 run 进程退出码是 5（interrupted）而不是 1（failed）**
（run 是后台任务，从它的完成通知里读退出码）

- [ ] **Step 5: 验证没在跑时也能用**

```bash
cat > $D/msg2.md <<'EOF'
再创建 s4.txt，内容为一行：second interject
EOF
python3 <仓库绝对路径>/codex_agent.py interrupt-and-resume iar-smoke-2026-09-19 \
  --brief $D/msg2.md --effort low
```
Expected: 打印「本来就没在跑，直接续跑」，`s4.txt` 出现

- [ ] **Step 6: 清理并提交冒烟结论**

```bash
rm -f ~/.codex-subagent/{tasks/iar-smoke-2026-09-19.json,reports/iar-smoke-2026-09-19.md,logs/iar-smoke-2026-09-19.log}
rm -rf $D
git commit --allow-empty -m "test: 端到端冒烟通过——打断留痕、退出码 5、上下文接上、空跑也能用"
```
