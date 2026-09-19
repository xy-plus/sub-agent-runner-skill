# `interrupt-and-resume` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 spec 里拆开的两块。**改动 A**：让判据说真话——轮次边界记下来而不是推测（A-1）、打断必留痕（A-2）、`interrupted` 是第五态（A-3）。**改动 B**：新命令 `interrupt-and-resume`，把「打断 → 确认退出 → 续跑」焊成一个动作，把那个每个调用方都要现编一遍的等待循环收进代码。B 依赖 A（没有 A，B 续跑之后被打断的那一轮仍会发出假的 `failed` 通知），所以一起交付，但分开验收。

**Architecture:** A-1 是一次结构性重构，先做：`run_codex` 回传本轮起始偏移，新增 `read_round(log_path, start_offset)` 与 `read_last_round(log_path)`，`judge` 改收**已经划好的本轮文本**而不是日志路径；本轮的拥有者（`cmd_run` / `_resume_round`）用自己的偏移，外部观察者（`cmd_status`）用最后一轮。A-2 把 `note_interrupt` 并进 `interrupt_codex`，三条打断路径全部走它。A-3 给 `_STATES` 加第五态。B 在此之上加 `wait_for_exit`、`check_can_resume`、`_resume_round` 和 `cmd_interrupt_and_resume`。

**Tech Stack:** Python 3 stdlib（`codex_agent.py` 新增 `import time`）。零第三方依赖。

## Global Constraints

- 对照 spec：`docs/superpowers/specs/2026-09-19-interrupt-and-resume-design.md`，每个决定以它为准。判据层级：**三条铁律 > 仓库规范 > 原始 SKILL.md**。
- **import 一律并到文件头**，两个文件都是。函数体里不许出现 `import`。
- **不设任何默认缺省值**，包括函数默认参数。超时／轮询间隔是**模块级常量**，由调用方显式传进去。
- 测试命令 `python3 -m unittest test_codex_agent -v`。**基线 116 个全绿**（已实跑确认）。每个任务的「预期通过数」在该任务里给出，并附上加减账，数对不上就说明写漏了或多写了，停下来核对，不要往下走。
- **空测试比没测试更糟。** 两条硬规矩：① 凡依赖外部进程／文件的测试，**先断言前提成立**；② 凡「断言常量等于那个常量自己」（`assertEqual(cmd(...), ca.EXIT["running"])`）一律视为空测试，要用**绝对值**。这个仓库在这上面栽过四次，四次都写在 `test_codex_agent.py` 的模块 docstring 里，动测试之前先读它。
- 凡需要 HOME 沙箱的测试类**一律继承已有的 `_HomeSandbox`**，不许再手写一份——它把 `Path.home()` 和 `$HOME` 两条路都 patch 了，手写的那份必然漏掉一条（`expanduser()` 走的是后者）。纯函数测试不要它。
- 知识写进代码注释，**贴在防住它的那行旁边**，不另开文档文件。spec 里带日期的实测数字必须落到对应注释里：INT→退出 1.854s／0.964s、被打断时已烧掉 28,107 tokens、父进程 4.06 秒才看到 banner、`stop` 路径 `INTERRUPT_MARK` 计数 0、0.83MB 日志判据耗时 7.6ms。
- 测试函数名用中文，沿用现有风格（`test_只认comm是codex的进程_别的进程不算`）。
- 每个任务末尾有**突变复验**步骤：把实现故意改坏、跑测试确认对应的那条**真的红**、再还原。改坏之后全绿 = 那条测试是空的，当场补。
- 每个任务提交一次，提交信息中文、说清楚**为什么**。提交时**不传 `-c user.email` / `-c user.name`**，用仓库已配好的身份。

---

### Task 1: 轮次边界记下来，不再从日志里推测（改动 A-1）

这是最大的一块，也必须第一个做：它改 `judge` 的签名，所有调用点和相关测试都会动。放在后面做，后面任务写的测试就要返工一遍。

**Files:** Modify `codex_agent.py` · Modify `test_codex_agent.py`

**Interfaces:**
- Consumes: 现有 `ROUND_MARK`、`strip_ansi`、`round_separator`、`_log_path`
- Produces（确切签名）：
  - `run_codex(kind: str, home: pathlib.Path, task: str, meta: dict, make_argv) -> Round` —— 返回**报告路径 + 本轮日志文本**这一对（内部用字节偏移切，偏移不外泄）
  - `read_round(log_path: pathlib.Path, start_offset: int) -> str` —— `[start_offset, 之后第一个 ROUND_MARK)`
  - `read_last_round(log_path: pathlib.Path) -> str` —— 外部观察者用
  - `runtime_error_lines(round_text: str) -> list` —— 不再自己切轮次，只认传进来的这一段
  - `judge(round: Round) -> Verdict` —— **只收那一对**，不收 pid（存活与否由调用方判），
    也不再让调用方自己把报告路径和文本凑到一起（凑错是静默算对的）
  - `has_interrupt_mark(text: str) -> bool` —— 痕迹判定走**整行**正则，不是子串
- Removes: `current_round(log_text)` —— 它的两个职责分别由 `read_round` / `read_last_round` 接走，留着就是第二个家

- [ ] **Step 1: 写失败的测试**

先把 `TestCurrentRound` 整个类改写成 `TestReadLastRound`（4 条，原 3 条 + 从 `TestJudge` 挪过来的 1 条）：

```python
class TestReadLastRound(unittest.TestCase):
    """外部观察者（status）只能看最后一轮——它手里没有偏移，这是它诚实的上界。"""

    def _log(self, text):
        p = pathlib.Path(tempfile.mkdtemp()) / "t.log"
        p.write_text(text)
        return p

    def test_只给最后一轮_上一轮的错误不算这一轮的(self):
        log = self._log(ca.round_separator("run", "t", "2026-09-19T10:00:00") + "\n"
                        + ERR_FATAL + "\n"
                        + ca.round_separator("resume", "t", "2026-09-19T11:00:00") + "\n干净收尾\n")
        self.assertEqual(ca.runtime_error_lines(ca.read_last_round(log)), [])

    def test_本轮自己的错误照样认(self):
        log = self._log(ca.round_separator("run", "t", "2026-09-19T10:00:00") + "\n干净\n"
                        + ca.round_separator("resume", "t", "2026-09-19T11:00:00") + "\n"
                        + ERR_FATAL + "\n")
        self.assertEqual(len(ca.runtime_error_lines(ca.read_last_round(log))), 1)

    def test_没有分隔符时返回全文_老日志和半路接手都还能判(self):
        log = self._log(ERR_FATAL + "\n")
        self.assertEqual(len(ca.runtime_error_lines(ca.read_last_round(log))), 1)

    def test_打断标记不能被当成新一轮的开始(self):
        # 它要是以 ROUND_MARK 开头，read_last_round 就从它这里切开，
        # 本轮前面的错误全被丢掉——判据当场失明
        self.assertFalse(ca.INTERRUPT_MARK.startswith(ca.ROUND_MARK))
        log = self._log(ca.round_separator("run", "t", "2026-09-19T10:00:00") + "\n"
                        + ERR_FATAL + "\n" + ca.INTERRUPT_MARK + "\n")
        self.assertEqual(len(ca.runtime_error_lines(ca.read_last_round(log))), 1)
```

再新增 `TestRoundBoundary`（6 条纯函数级）：

```python
class TestRoundBoundary(unittest.TestCase):
    """一轮的边界是**记下来的事实**，不是从日志里往回猜出来的。

    病根：judge 过去按最后一个 ROUND_MARK 往回切，而日志是多个进程共写的。
    interrupt-and-resume 一确认 codex 退出就调 run_codex 往同一个日志追加新的
    ROUND_MARK，而被打断那一轮的 run 包装器此刻正要跑判据——谁先谁后没有任何
    保证，实测两者落在同一秒内。包装器晚一步，INTERRUPT_MARK 就被切到本轮之外，
    同一份日志上的结论从「被 INT 打断，接着 resume」翻成「报告缺失＝没正常收尾」。
    判据成本还随日志增长（0.83MB 时 7.6ms），长任务上天平继续朝竞争方倾斜，
    而长任务正是最该打断、上下文最值钱的场景。
    """

    def _home(self):
        d = pathlib.Path(tempfile.mkdtemp())
        for sub in ("tasks", "reports", "logs"):
            (d / sub).mkdir()
        return d

    def _spawn_writing(self, log, *chunks):
        """假 codex：spawn 的那一刻往日志追加这几段。

        **必须在 run_codex 返回之前写**——本轮文本的快照就在它返回那一刻取走。
        写在 with 块外面的话，右端截断那条根本测不到。
        """
        def spawn(*a, **k):
            with open(log, "ab") as f:
                for c in chunks:
                    f.write(c.encode())
            return mock.DEFAULT
        return spawn

    def test_run_codex回传的就是本轮的日志文本_上一轮的不带进来(self):
        d = self._home()
        log = ca._log_path(d, "t")
        log.write_bytes("上一轮的尾巴\n".encode())
        with _no_codex() as popen:
            popen.side_effect = self._spawn_writing(log, "本轮的内容\n")
            text = ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertEqual(text, "本轮的内容\n")

    def test_后来的轮次不能把前一轮判瞎_这是P1的回归锁(self):
        d = self._home()
        log, report = ca._log_path(d, "t"), ca._report_path(d, "t")
        # 本轮被 INT 打断（只留痕、没有报告），而判据还没跑，interrupt-and-resume
        # 已经把下一轮的分隔符追了进去——实测两者落在同一秒内。
        with _no_codex() as popen:
            popen.side_effect = self._spawn_writing(
                log, ca.INTERRUPT_MARK + "\n",
                ca.round_separator("interrupt-and-resume", "t", "2026-09-19T00:00:01")
                + "\n干净收尾\n")
            round_text = ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertIn(ca.INTERRUPT_MARK, log.read_text(), "前提不成立：打断标记没写进日志")

        self.assertEqual(ca.judge(report, round_text).state, "interrupted")
        # 反面钉一条：猜边界（只看最后一轮）在这里当场失明——证明上面那条真的在挡东西
        self.assertNotEqual(ca.judge(report, ca.read_last_round(log)).state, "interrupted")

    def test_区间右端截到下一个分隔符_后一轮的内容不许被吞进前一轮(self):
        d = self._home()
        log = ca._log_path(d, "t")
        with _no_codex() as popen:
            popen.side_effect = self._spawn_writing(
                log, "本轮干净\n",
                ca.round_separator("resume", "t", "2026-09-19T00:00:01") + "\n",
                ERR_FATAL + "\n")
            text = ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertEqual(text, "本轮干净\n")
        self.assertEqual(ca.runtime_error_lines(text), [],
                         "后一轮的致命错误被算到了前一轮头上")

    def test_切的是字节不是字符_中文日志不许错位(self):
        # 日志里全是中文：brief 原文、codex 的中文输出。按字符切会整体错位，
        # 切出来的开头是半截字节——判据读到的「本轮」根本不是本轮。
        d = self._home()
        log = ca._log_path(d, "t")
        log.write_bytes("上一轮写了很多中文内容\n".encode())
        with _no_codex() as popen:
            popen.side_effect = self._spawn_writing(log, "本轮第一行\n")
            text = ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        raw = log.read_bytes()
        self.assertNotEqual(len(raw), len(raw.decode()),
                            "前提不成立：日志里没有多字节字符，这条测不到错位")
        self.assertEqual(text, "本轮第一行\n")

    def test_日志还没有时读回空串_不许抛(self):
        d = self._home()
        missing = ca._log_path(d, "从来没写过")
        self.assertFalse(missing.exists(), "前提不成立：这个文件居然存在")
        self.assertEqual(ca.read_round(missing, 0), "")
        self.assertEqual(ca.read_last_round(missing), "")

    def test_半截多字节字符不许把读取打崩(self):
        # codex 被 INT 打断时可能只写出半截字节，裸 decode 会把判据整个打崩
        d = self._home()
        log = ca._log_path(d, "t")
        log.write_bytes(b"\xff\xfe" + "正常收尾".encode())
        self.assertIn("正常收尾", ca.read_last_round(log))
        self.assertIn("正常收尾", ca.read_round(log, 0))
```

再新增 `TestRoundBoundaryWiring`（4 条，钉**接线**——光把函数写对、调用点接错，bug 原样还在）：

```python
class TestRoundBoundaryWiring(_HomeSandbox):
    """谁用哪种边界，是这次改动的全部意义所在。

    单元层的 read_round 全绿、cmd_run 却接成 read_last_round —— bug 一点没修。
    所以四条各钉一条：run 和 resume 用 run_codex 回传的本轮文本、status 不在跑时
    用最后一轮、status **还在跑时根本不读日志**（judge 那一支用不到它，而 status
    是轮询用的热路径）。
    """

    def setUp(self):
        super().setUp()
        self.workdir = self.home / "repo"
        self.workdir.mkdir()
        self.brief = self.home / "brief.md"
        self.brief.write_text("干活")

    def _race(self, d):
        """假 codex：本轮被打断（只留痕、不留报告），紧接着下一轮的分隔符抢先落地。"""
        def spawn(*a, **k):
            with open(ca._log_path(d, "t"), "ab") as f:
                f.write((ca.INTERRUPT_MARK + "\n").encode())
                f.write((ca.round_separator("interrupt-and-resume", "t", "2026-09-19T00:00:01")
                         + "\n干净收尾\n").encode())
            return mock.DEFAULT
        return spawn

    def test_run收尾用的是自己的偏移(self):
        d = ca.ensure_isolation("default")
        args = ca.build_parser().parse_args(
            ["run", "--task", "t", "--dir", str(self.workdir), "--brief", str(self.brief),
             "--effort", "low", "--account", "default"])
        seen = {}
        with _no_codex() as popen, \
             mock.patch.object(ca, "_print_verdict", side_effect=lambda t, v: seen.update(v=v)):
            popen.side_effect = self._race(d)
            ca.cmd_run(args)
        self.assertIn("resume", seen["v"].reason,
                      "run 接成了 read_last_round：被后来的一轮判瞎了")

    def test_resume收尾用的是自己的偏移(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t", session_id="s1", dir=str(self.workdir)))
        args = ca.build_parser().parse_args(
            ["resume", "t", "--brief", str(self.brief), "--effort", "low"])
        seen = {}
        with _no_codex() as popen, \
             mock.patch.object(ca, "find_codex_pid", return_value=None), \
             mock.patch.object(ca, "_print_verdict", side_effect=lambda t, v: seen.update(v=v)):
            popen.side_effect = self._race(d)
            ca.cmd_resume(args)
        self.assertIn("resume", seen["v"].reason)

    def test_status用的是最后一轮_外部观察者只能看最后一轮(self):
        # 反向钉一道：status 没有偏移可用，它只能看最后一轮，也**只该**看最后一轮。
        # 接成别的（比如从头扫）会把上一轮的打断标记算到这一轮头上。
        #
        # spy 的是 judge 而不是 _print_verdict：cmd_status 自己排版、根本不走
        # _print_verdict（patch 它只会拿到空列表，seen[0] 当场 IndexError，
        # 是条连跑都跑不起来的空测试）。spy 还顺手把**传给判据的那段文本**也
        # 钉住了——这正是「谁用哪种边界」的本体。
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t"))
        log = ca._log_path(d, "t")
        log.write_text(ca.round_separator("run", "t", "2026-09-19T00:00:00") + "\n"
                       + ca.INTERRUPT_MARK + "\n"
                       + ca.round_separator("resume", "t", "2026-09-19T00:00:01") + "\n干净收尾\n")
        args = ca.build_parser().parse_args(["status", "t"])
        seen, texts, real_judge = [], [], ca.judge

        def spy(report_path, round_text):
            texts.append(round_text)
            v = real_judge(report_path, round_text)
            seen.append(v)
            return v

        with mock.patch.object(ca, "find_codex_pid", return_value=None), \
             mock.patch.object(ca, "judge", side_effect=spy):
            ca.cmd_status(args)
        self.assertEqual(len(seen), 1, "前提不成立：judge 没被调到，这条什么都没测")
        self.assertNotIn(ca.INTERRUPT_MARK, texts[0], "status 把上一轮的打断标记读进了本轮")
        self.assertNotIn("resume", seen[0].reason,
                         "上一轮的打断标记被算到了这一轮头上")
```

最后把 `TestJudge` 改成收文本（15 条 → 12 条：`test_打断标记不能被当成新一轮的开始` 挪去 `TestReadLastRound`、`test_非UTF8的日志不许把判据打崩` 挪去 `TestRoundBoundary`、`test_PID还活着就是running_不看产物` 随 `pid` 参数一起删——它要保的那件事已由 `TestExitCodeContract` 里 **status 还在跑退出 4** 那条端到端钉着，更硬）：

```python
class TestJudge(unittest.TestCase):
    """判据只认**已经划好的本轮文本**。边界谁来划，见 TestRoundBoundary。"""

    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.report = self.d / "t.md"

    def _judge(self, round_text="正常收尾\n"):
        return ca.judge(self.report, round_text)

    def test_报告缺失是failed(self):
        v = self._judge()
        self.assertEqual(v.state, "failed")
        self.assertIn("没正常收尾", v.reason)

    def test_报告为空也是failed(self):
        self.report.write_text("")
        self.assertEqual(self._judge().state, "failed")

    def test_报告缺失且撞额度上限_reason要点名(self):
        v = self._judge(ERR_USER_LAYER + "\n")
        self.assertEqual(v.state, "failed")
        self.assertIn("额度", v.reason)

    def test_报告是散文也算success_并预览前几行(self):
        # 实测 156 份真实报告只有 4 份能解析成 JSON：-o 写的是 agent 的最后一条
        # 消息，通常是 markdown 散文。报告里该有什么字段是任务层的事。
        self.report.write_text("干完了，见分支 feat/x\n改了 3 个文件\n测试全绿\n")
        v = self._judge()
        self.assertEqual(v.state, "success")
        self.assertEqual(v.detail[0], "干完了，见分支 feat/x")

    def test_预览最多几行_长报告不刷屏(self):
        self.report.write_text("\n".join(f"第 {i} 行" for i in range(50)))
        v = self._judge()
        self.assertEqual(len(v.detail), ca.REPORT_PREVIEW_LINES)
        self.assertLess(len(v.detail), 50)   # 钉住「确实截断了」，不随常量一起动

    def test_报告在但本轮日志有未分类错误是suspect(self):
        self.report.write_text("干完了")
        v = self._judge("2026-09-17T14:35:32.578919Z ERROR codex_core::session: "
                        "Failed to create session: thread-store conflict\n")
        self.assertEqual(v.state, "suspect")
        self.assertEqual(len(v.detail), 1)

    def test_撞上写锁_reason要点名(self):
        v = self._judge(ERR_FATAL + "\n")
        self.assertEqual(v.state, "failed")
        self.assertIn("锁", v.reason)

    def test_清报告之后旧内容不会被当成本轮产物(self):
        # codex 只在正常收尾时写 -o、启动时不 truncate。不清掉的话，一次秒死于
        # 写锁的 resume 会读到上一轮的报告并被判成 success——工具在说谎。
        self.report.write_text("上一轮的报告")
        ca.clear_report(self.report)
        self.assertEqual(self._judge().state, "failed")

    def test_清报告对还没有报告的任务也成立(self):
        ca.clear_report(self.report)   # 不存在也不许抛

    def test_被INT打断的那轮_reason要告诉人可以resume(self):
        """2026-09-19 真机复现：`stop t` 刚打印完「上下文保留，可 resume」，
        紧接着 `status t` 就说 `failed —— 报告缺失或为空＝没正常收尾`，退出码 1。
        两句话自相矛盾，而调用方拿不到那条唯一有用的信息。
        """
        v = self._judge(ca.INTERRUPT_MARK + "\n")
        self.assertEqual(v.state, "failed")     # 状态这一维在 Task 2 才改
        self.assertIn("resume", v.reason)

    def test_撞额度上限比被打断更该被说出来(self):
        # 两个都命中时，「换账号」比「可以 resume」更接近真正的处置
        self.assertIn("额度", self._judge(ERR_USER_LAYER + "\n" + ca.INTERRUPT_MARK + "\n").reason)

    def test_非UTF8的报告不许把判据打崩(self):
        # codex 被 SIGINT 打断时可能只写出半截字节
        self.report.write_bytes(b"\xff\xfe" + "干完了".encode())
        self.assertEqual(self._judge().state, "success")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestRoundBoundary test_codex_agent.TestReadLastRound -v`

Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'read_round'`（`read_last_round` 同）

- [ ] **Step 3: 最小实现**

`codex_agent.py` 里把 `current_round` 整个换掉：

```python
def read_round(log_path, start_offset):
    """读日志的 `[start_offset, 之后第一个 ROUND_MARK)` 这一段——本轮，且只有本轮。

    `start_offset` 是 `run_codex` 写完本轮分隔符之后回传的**字节**偏移，
    是 codex 还没起跑之前就拿到的事实，**后来的任何一轮都不可能把它致盲**。
    这替掉了「按最后一个 ROUND_MARK 往回猜」那套：日志是多个进程共写的，
    interrupt-and-resume 一确认退出就往同一个日志追加新分隔符，而被打断那一轮的
    包装器此刻正要跑判据——谁先谁后没有任何保证，实测两者落在同一秒内。
    包装器晚一步，同一份日志上的结论就从「被 INT 打断，接着 resume」
    翻成「报告缺失＝没正常收尾」。

    **偏移是字节不是字符**，所以这里走二进制 seek 再 decode：日志里全是中文
    （brief 原文、codex 的中文输出），按字符切会整体错位。偏移永远落在
    分隔符那行的 `\\n` 之后，不会切在多字节字符中间。
    `errors="replace"`：codex 被 INT 打断时可能只写出半截字节，裸 decode 会把
    判据整个打崩。
    右端截到「起点之后的第一个 ROUND_MARK」，所以后一轮的内容也不会被吞进来。
    """
    if not log_path.exists():
        return ""
    with open(log_path, "rb") as f:
        f.seek(start_offset)
        raw = f.read()
    text = strip_ansi(raw.decode("utf-8", "replace"))
    cut = text.find(ROUND_MARK)
    return text if cut < 0 else text[:cut]


def read_last_round(log_path):
    """最后一轮。**只给外部观察者用**（`status`）——它手里没有偏移。

    这是它诚实的上界：它本来就只能看到最后一轮，说不出更多。
    本轮的拥有者（cmd_run / _resume_round）绝不该用它：拥有者手里有事实，
    用这个就等于把事实换回推测。
    """
    if not log_path.exists():
        return ""
    text = strip_ansi(log_path.read_text(errors="replace"))
    cut = text.rfind(ROUND_MARK)
    return text if cut < 0 else text[cut:]
```

`runtime_error_lines` 只改开头一行（参数名和取文本的方式），其余不动：

```python
def runtime_error_lines(round_text):
    """本轮日志里 codex 自己的错误行（已滤掉良性 target 与良性用户层消息）。

    **本轮的边界由调用方划好再传进来**，本函数不再自己切——切法有两种
    （拥有者用偏移、观察者用最后一轮），藏在这里面就只剩「猜」一种。

    这里仍然 strip_ansi 一道：它是幂等的，而少了它，一个直接拿原始日志文本
    调进来的人会静默拿到空结果（`^ERROR:` 匹配不到 `\\x1b[31mERROR:`）——
    静默的错比多一次正则扫描贵得多。

    刻意没有「只看末 N 行」的窗口参数。那个窗口过去偷偷承担着「运行中已经恢复
    过去的错误不算」这个语义，而这件事现在由 target 白名单正经做了。
    """
    hits = []
    for raw in strip_ansi(round_text).splitlines():
        ...   # 以下三段匹配逻辑原样不动
```

`judge` 换签名：

```python
def judge(report_path, round_text):
    """唯一的成败判据——**只看产物和本轮日志**。`run`／`resume` 收尾和 `status`
    共用它，避免两处判据漂移。

    **本轮的日志文本由调用方划好再传进来**，判据自己不划边界。划边界的两种人
    不一样：本轮的拥有者拿 `run_codex` 回传的本轮文本，外部观察者只能看最后一轮
    （`read_last_round`）。把这件事塞回 judge 里，就只剩「猜」一种做法，
    而那正是这次要修掉的整类 bug。

    **存活与否不在这里判**：那是进程的事，不是产物的事。三个调用点里有两个
    恒传 `None`，说明它本来就属于剩下那一家（status）。留着它还要白烧：
    `pid is not None` 那一支根本不碰 round_text，读出来直接丢，而 status 的
    热路径恰恰是轮询**还在跑**的任务。

    codex 的退出码不可信：中途已恢复的工具 ERROR（apply_patch 被拒后重打成功）
    也会把退出码染成 1。所以判据只看产物和日志，不看退出码。
    """
    errors = runtime_error_lines(round_text)
    # errors="replace"：codex 被 SIGINT 打断时可能只写出半截字节，
    # 裸 read_text 会 UnicodeDecodeError 把判据整个打崩。
    report_text = report_path.read_text(errors="replace") if report_path.exists() else ""
    ...   # 以下分支逻辑原样不动，只是把原先的 round_text 局部变量换成参数
```

`run_codex` 回传偏移（只动两处）：

```python
    with open(_log_path(home, task), "ab") as log:
        log.write((round_separator(kind, task, _now_iso()) + "\n").encode())
        log.flush()
        # 本轮的起点：分隔符之后的第一个字节。
        # O_APPEND 下每次 write 都是「原子地跳到末尾再写」，所以即使别的进程
        # 正往同一个日志追加（留痕、另一轮的分隔符），这个位置依然精确
        # 指向**我们自己刚写的那行之后**——拥有者的边界由此成为事实而非推测。
        start_offset = log.tell()

        ...   # spawn、信号转发、tee 全部原样不动

    # 回传**本轮的日志文本**而不是偏移。偏移是可以被悄悄丢掉的：调用方忘了接，
    # 唯一还能拿到本轮文本的路就是 read_last_round——正好是这次要修的那个 bug。
    # 文本丢不掉，它就是 judge 的参数。（铁律 2：把约束做进签名本身。）
    # 副带好处：快照在 codex 退出那一刻取走，比「调用方稍后自己读」窗口更小。
    return read_round(_log_path(home, task), start_offset)
```

三个调用点：

```python
# cmd_run 里
    verdict = judge(report,
                    run_codex("run", home, args.task,
                              new_meta(args.task, args.account, str(workdir), args.effort),
                              lambda r: build_run_argv(str(workdir), args.effort, r, brief)))

# cmd_resume 里（Task 5 会把这段抽成 _resume_round，这里先就地改）
    verdict = judge(report,
                    run_codex("resume", home, args.task, meta,
                              lambda r: build_resume_argv(meta["dir"], meta["session_id"],
                                                          args.effort, r, brief)))

# cmd_status 里：外部观察者。还在跑就根本不读日志——判据在那一支用不到它，
# 而 status 是轮询用的热路径（实测单份日志 0.83MB 读+strip_ansi 约 10ms、
# 8.3MB 约 100ms，列全部任务还要乘任务数）。
        pid = find_codex_pid(report)
        verdict = (Verdict("running", f"pid={pid} 存活", []) if pid is not None
                   else judge(report, read_last_round(log)))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`

Expected: PASS，**124 个全绿**。加减账：116 − 3（`TestCurrentRound` 整类删掉）− 15（`TestJudge` 整类重写）＋ 4（`TestReadLastRound`）＋ 12（新 `TestJudge`）＋ 6（`TestRoundBoundary`）＋ 4（`TestRoundBoundaryWiring`）＝ 124。

- [ ] **Step 5: 突变复验**（三个突变逐个做，做完还原）

1. `read_round` 的右端截断去掉，改成 `return text`
   → `test_区间右端截到下一个分隔符_后一轮的内容不许被吞进前一轮` 必须红
2. `read_round` 改成按字符切：`return strip_ansi(log_path.read_text(errors="replace"))[start_offset:]`
   → `test_偏移是字节数不是字符数_中文日志不许错位` 必须红
3. `cmd_run` 里的 `read_round(..., start_offset)` 改回 `read_last_round(...)`
   → `test_run收尾用的是自己的偏移` 必须红

三条都红 = 边界这件事真的被钉住了。任何一条绿，说明那条测试是空的，当场补。

- [ ] **Step 6: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "fix: 轮次边界记下来而不是推测——后来的一轮不能再把前一轮判瞎

run_codex 回传本轮起始偏移，拥有者用 read_round 拿事实，外部观察者用
read_last_round 拿它唯一看得见的东西。judge 不再自己猜边界。
病根是日志多进程共写：interrupt-and-resume 一确认退出就追加新分隔符，
而被打断那一轮的包装器正要跑判据，实测两者落在同一秒内。"
```

---

### Task 1b: 边界的右端也必须是事实（代码审查 Critical-1）

左端改成记下来的字节偏移之后，右端仍是 `text.find(ROUND_MARK)`／`text.rfind(...)`、
痕迹判定仍是 `INTERRUPT_MARK in round_text`——**全是子串搜索，全是推测**。
模块 docstring 自己写着「日志里还混着 brief 原文和 codex 转述的子进程输出」，
而这个仓库的日常就是派 codex 来改 `codex_agent.py` 自己。

| 日志里混进的源码行 | 后果 |
|---|---|
| `ROUND_MARK = "===== codex-agent "` | 本轮文本被切剩 14 个字符 → `interrupted` 翻成 **`failed`** |
| `INTERRUPT_MARK = "----- codex-agent 本轮被 INT 打断…"` | `failed` 翻成 **`interrupted`**、退出码 130 → agent 去 resume 一个根本没被打断的失败轮 |

**Files:** Modify `codex_agent.py` · Modify `test_codex_agent.py`

- [ ] **Step 1: 写失败的测试**（两条回归，直接拿源码行当样本）

```python
    def test_日志里混进ROUND_MARK的源码行_不许被当成轮次分隔符(self):
        # 这个仓库的日常就是派 codex 改 codex_agent.py 自己，源码行进日志是常态。
        # 子串搜索会在这里切断本轮：切剩 'ROUND_MARK = "' 14 个字符，
        # 打断标记被甩到本轮之外，judge 从 interrupted 翻成 failed。
        d = self._home()
        log = ca._log_path(d, "t")
        源码行 = 'ROUND_MARK = "===== codex-agent "   # 每轮开跑前写进日志的分隔符前缀'
        with _no_codex() as popen:
            popen.side_effect = self._spawn_writing(
                log, 源码行 + "\n", ca.INTERRUPT_MARK + "\n")
            rd = ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertIn(ca.ROUND_MARK, 源码行, "前提不成立：样本行里没有分隔符前缀")
        self.assertIn(源码行, rd.text, "本轮文本被那行源码切断了")
        self.assertEqual(ca.judge(rd).state, "interrupted")

    def test_日志里混进INTERRUPT_MARK的源码行_不许被当成真打断(self):
        # 反方向：一轮真正失败的运行，日志里恰好转述了那行常量定义，
        # 子串搜索会判成 interrupted、退出码 130，
        # 而照契约做决定的 agent 会去 resume 一个根本没被打断的失败轮。
        d = self._home()
        log = ca._log_path(d, "t")
        源码行 = f'INTERRUPT_MARK = "{ca.INTERRUPT_MARK}"'
        with _no_codex() as popen:
            popen.side_effect = self._spawn_writing(log, 源码行 + "\n")
            rd = ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertIn(ca.INTERRUPT_MARK, 源码行, "前提不成立：样本行里没有打断标记")
        self.assertEqual(ca.judge(rd).state, "failed")
```

- [ ] **Step 2/3: 整行匹配**

```python
# 分隔符与痕迹一律**整行**匹配，不是子串。
# 子串搜索在这个仓库里是真会说谎的：日志里混着 brief 原文和 codex 转述的子进程
# 输出，而这里的日常就是派 codex 来改 codex_agent.py 自己——源码里这两行常量
# 定义一旦被转述进日志，两个方向都会翻车（见 TestRoundBoundary 里那两条回归）。
# round_separator 产出的 kind/task/when_iso 三段都不含空格（kind 是枚举、任务名
# 字符集排除空格、isoformat(timespec="seconds") 无空格），所以 \S+ \S+ \S+ 精确。
_ROUND_LINE = re.compile(r"^" + re.escape(ROUND_MARK) + r"\S+ \S+ \S+ =====$", re.M)
# 痕迹行尾可以跟一个 [来源]，见 interrupt_codex 的 cause。
_MARK_LINE = re.compile(r"^" + re.escape(INTERRUPT_MARK) + r"(\s\[.*\])?$", re.M)


def has_interrupt_mark(text):
    """本轮是否真的被打断。整行匹配，不是子串——理由见 _ROUND_LINE。"""
    return _MARK_LINE.search(text) is not None
```

四个调用点：`read_round` 用 `_ROUND_LINE.search` 取右端、`read_last_round` 用
`finditer` 取最后一个、`judge` 与 `cmd_interrupt_and_resume` 改用 `has_interrupt_mark`。

- [ ] **Step 4: 突变复验**

两条回归各自对应一个方向，把整行正则改回子串搜索 → 两条必须都红。

---

### Task 2: `interrupted` 是第五态（改动 A-3）

**Files:** Modify `codex_agent.py` · Modify `test_codex_agent.py`

**Interfaces:**
- Consumes: Task 1 的 `judge(round)`
- Produces:
  - `_STATES = (("success", 0), ("running", 4), ("interrupted", 130), ("suspect", 3), ("failed", 1))`
  - `EXIT["interrupted"] == 130`（＝128+SIGINT，跟既成约定，见下）；`_SEVERITY == ["success", "running", "interrupted", "suspect", "failed"]`
  - `judge` 认出 `INTERRUPT_MARK` 时返回 state `"interrupted"`

- [ ] **Step 1: 写失败的测试**

```python
class TestInterruptedIsItsOwnState(unittest.TestCase):
    """被打断的轮次该做的事是**接着 resume**，和其余四态都不同，所以它是第五个状态。

    只把 reason 写对是不够的：harness 的完成通知**只搬退出码，不搬 stdout**，
    reason 字符串再准确也到不了做决定的那一方；能到的只有那个数字。而在这唯一
    到得了的通道上，interrupted 和 failed 目前是同一个值，处置却相反。
    损失可量化：审查的探针被打断时已烧掉 28,107 tokens，退出码 1 会让照 SKILL.md
    契约做决定的 agent 从头重跑，那 28k 连同保住的上下文一起扔掉。
    """

    def setUp(self):
        self.report = pathlib.Path(tempfile.mkdtemp()) / "t.md"

    def test_五个状态的退出码逐个钉死(self):
        # 绝对值。写成 EXIT["x"] == EXIT["x"] 那种自指是空测试，本仓栽过。
        self.assertEqual(ca.EXIT, {"success": 0, "failed": 1, "suspect": 3,
                                   "running": 4, "interrupted": 130})

    def test_interrupted跟的是128加信号号这个既成约定(self):
        """130 不是随手挑的数：128 + SIGINT(2)，POSIX/Bash 的既成约定
        （同族 SIGKILL→137、SIGTERM→143）。钉住这个**算式**而不只是 130，
        下一个人就改不成一个「看起来也挺顺」的数。
        """
        self.assertEqual(ca.EXIT["interrupted"], 128 + int(signal.SIGINT))

    def test_护栏拒绝的码不与任何判据结论相撞(self):
        self.assertEqual(ca.USAGE_ERROR, 2)
        self.assertNotIn(ca.USAGE_ERROR, set(ca.EXIT.values()))

    def test_严重度顺序的绝对值(self):
        self.assertEqual(ca._SEVERITY,
                         ["success", "running", "interrupted", "suspect", "failed"])

    def test_worse在running和interrupted之间取interrupted(self):
        """单独钉一条——少钉一对，_SEVERITY 就能被悄悄重排。

        在跑的任务会自己好，**被打断的永远不会自己好**：它在等人动手。
        status 列一批任务时若被 running 盖住，调用方会去「等」一个
        永远不会自己好的东西。
        """
        self.assertEqual(ca._worse("running", "interrupted"), "interrupted")
        self.assertEqual(ca._worse("interrupted", "running"), "interrupted")

    def test_有打断标记且无报告时状态是interrupted(self):
        v = ca.judge(Round(self.report, ca.INTERRUPT_MARK + "\n"))
        self.assertEqual(v.state, "interrupted")
        self.assertEqual(ca.EXIT[v.state], 5)
        self.assertIn("resume", v.reason)

    def test_额度上限和写锁仍然是failed_它们resume救不回来(self):
        # 这两条的补救是换账号／新起任务，不是 resume——不许被第五态顺手吃掉
        for mark in (ca.USAGE_LIMIT_MARK, ca.THREAD_LOCK_MARK):
            with self.subTest(mark=mark):
                v = ca.judge(Round(self.report, mark + "\n" + ca.INTERRUPT_MARK + "\n"))
                self.assertEqual(v.state, "failed")
                self.assertEqual(ca.EXIT[v.state], 1)

    def test_打断与错误行共存时状态取interrupted_错误行照常进detail(self):
        """被 INT 打断几乎必然留下
        `ERROR codex_core::session: failed to record rollout items: thread … not found`，
        而 codex_core::session 刻意不在良性白名单里（它在非打断场景下仍该被看见）。
        优先级：状态取 interrupted（处置是 resume），错误行照常进 detail。
        """
        # 前提：这条错误行不许自带额度／写锁标记，否则命中的是上面那条分支，
        # 这条测试就悄悄变成 test_额度上限和写锁仍然是failed 的副本。
        # （ERR_TRACING_UNKNOWN 正是这样：它自带 "already has an active writer"。
        #   改用 codex 被 INT 打断时真正留下的那一行。）
        for other in (ca.USAGE_LIMIT_MARK, ca.THREAD_LOCK_MARK):
            self.assertNotIn(other, ERR_ROLLOUT_ON_INTERRUPT,
                             "前提不成立：样本行自带更坏的标记，这条测的不是共存优先级")
        v = ca.judge(self.report, ERR_ROLLOUT_ON_INTERRUPT + "\n" + ca.INTERRUPT_MARK + "\n")
        self.assertEqual(v.state, "interrupted")
        self.assertEqual(len(v.detail), 1)
        self.assertIn("codex_core::session", v.detail[0])
```

再往 `TestExitCodeContract` 里补一条——**第五态唯一的端到端绝对值**。
上面那些都在判据层，而退出码是**唯一到得了调用方的通道**，
「判据说 interrupted」和「进程真的 exit 130」是两件事：

```python
    def test_run_被打断退出130(self):
        # 这条是整个改动 A 的验收点：harness 的完成通知只搬退出码，
        # 于是这个数字是「接着 resume，别重跑」唯一到得了调用方的形式。
        self.assertEqual(self._run(None, ca.INTERRUPT_MARK), 130)
```

同时改两条已有的：

- `TestJudge.test_被INT打断的那轮_reason要告诉人可以resume`：`assertEqual(v.state, "failed")` 改成 `"interrupted"`，注释里那句「状态这一维在 Task 2 才改」删掉，换成推翻旧决定的理由（见下）。
- `TestExitCodeContract.test_退出码的绝对值是对外契约`：字典加 `"interrupted": 130`。

**`SKILL.md` 那两条文档断言（退出码 `130`、第五条命令）留到 Task 7 一起改** —— 提前改会让本任务的全量测试红。

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestInterruptedIsItsOwnState -v`

Expected: FAIL，`{'success': 0, 'running': 4, 'suspect': 3, 'failed': 1} != {...'interrupted': 130}`

- [ ] **Step 3: 最小实现**

```python
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
# 顺序即严重度（越靠后越该拦住调用方）。**interrupted 必须排在 running 之后**：
# 在跑的任务会自己好，被打断的永远不会自己好——它在等人动手。status 列一批任务
# 时若被 running 盖住，调用方会去「等」一个永远不会自己好的东西。
# 码值与顺序无关，2 永久留给护栏拒绝（USAGE_ERROR）。
#
# interrupted 用 **130** 而不是自编一个数：128+SIGINT(2) 是 POSIX/Bash 的既成
# 约定（同族 SIGKILL→137、SIGTERM→143），脚本作者和 agent 不读本文档也认得。
# 反面要知道：>128 在约定里指「**本进程**死于信号」，而这里死的是 codex、
# 包装器是正常退出的。取它是因为读者的第一反应正确（「被打断了，接着来」），
# 比语义上的精确更重要——退出码是唯一能到达 harness 完成通知的通道。
# 3/4 仍是自编：suspect、running 这两个概念约定里根本没有。
# 原则是：**能跟约定的跟，约定没涵盖的才自己编。**
_STATES = (("success", 0), ("running", 4), ("interrupted", 130), ("suspect", 3), ("failed", 1))
```

`judge` 里那一支：

```python
        # 排在额度上限和写锁之后：那两条意味着 resume 也救不回来（换账号／新起
        # 任务），而这一条恰恰是「resume 就行」，不能把更坏的消息盖掉。
        # 日志里同时有 codex_core::session 的错误行是常态（被 INT 打断几乎必然
        # 留下 failed to record rollout items），那些照常进 detail，不改状态。
        if INTERRUPT_MARK in round_text:
            return Verdict("interrupted",
                           "本轮被 INT 打断，上下文保留——接着 resume 即可，不用重跑", errors)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`

Expected: PASS，**133 个全绿**（124 + `TestInterruptedIsItsOwnState` 8 条 + `test_run_被打断退出130` 1 条）。

- [ ] **Step 5: 突变复验**

1. `_STATES` 里把 `("interrupted", 130)` 挪到 `("running", 4)` **之前**
   → `test_严重度顺序的绝对值` 和 `test_worse在running和interrupted之间取interrupted` 必须都红
2. `EXIT["interrupted"]` 的码值从 130 改成 1
   → `test_五个状态的退出码逐个钉死` 和 `test_有打断标记且无报告时状态是interrupted` 必须红

还原。

- [ ] **Step 6: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "fix: 被打断的轮次是第五态 interrupted，退出码 130

harness 的完成通知只搬退出码不搬 stdout，reason 写得再准也到不了做决定的
那一方；而在那个唯一的通道上 interrupted 和 failed 此前是同一个数字、处置
却相反。实测代价 28,107 tokens 白烧。
严重度排在 running 之后：在跑的会自己好，被打断的永远不会自己好。"
```

---

### Task 3: 发 INT 与留痕焊成一个动作（改动 A-2）

**Files:** Modify `codex_agent.py` · Modify `test_codex_agent.py`

**Interfaces:**
- Consumes: `INTERRUPT_MARK`、`_log_path`
- Produces: `interrupt_codex(pid: int, log_path: pathlib.Path, cause: str) -> None` —— **刻意不给返回值**；`cause` 必填，追在痕迹行尾（见 spec「痕迹要带来源」）
- Removes: `note_interrupt(log_path)` —— 整个并进 `interrupt_codex`，它的知识（O_APPEND 原子写、写不进去就算了）一并搬进去
- 三个调用点全部走它：`forward_as_sigint`、`cmd_stop`、Task 5 的新命令

- [ ] **Step 1: 写失败的测试**

```python
class TestInterruptCodex(unittest.TestCase):
    """发 INT 和留痕是同一事件的两面，拆开就是一句「记得也写一下标记」的软约定。

    而它**已经漏过一次**：cmd_stop 用裸 os.kill 打给 codex，写痕迹的
    note_interrupt 只在包装器自己的信号处理器里被调用，于是 stop 这条路上
    痕迹永远不写。2026-09-19 实测的三行：
        stop 打印:  已向 rv-probe1 (pid=602545) 发 SIGINT，上下文保留，可 resume
        包装器收尾: failed —— 报告缺失或为空＝没正常收尾        ← 退出码 1
        日志里 INTERRUPT_MARK 计数: 0
    """

    def setUp(self):
        self.log = pathlib.Path(tempfile.mkdtemp()) / "t.log"
        self.log.write_text("")

    def test_发出信号的同时一定留痕(self):
        # 陪练进程必须**先报到再挨打**：Popen 一返回就发 INT 的话，信号会落在
        # 解释器启动途中，那时 SIGINT 还是 SIG_DFL，进程直接被信号打死
        # （returncode -2），于是「它是被 INT 正常收走的」这条断言测的其实是
        # 一场竞态而不是本函数。
        ready = self.log.parent / "陪练就位.txt"
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import pathlib,signal,sys,time\n"
                                 "signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))\n"
                                 "pathlib.Path(sys.argv[1]).write_text('ok')\n"
                                 "time.sleep(30)", str(ready)])
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not ready.exists():
                time.sleep(0.01)
            self.assertTrue(ready.exists(),
                            "前提不成立：陪练进程没装上 INT 处理器，这条测不到「被 INT 正常收走」")
            self.assertTrue(ca.pid_alive(proc.pid), "前提不成立：陪练进程没起来")
            ca.interrupt_codex(proc.pid, self.log)
            self.assertIn(ca.INTERRUPT_MARK, self.log.read_text())
            proc.wait(timeout=5)
            self.assertEqual(proc.returncode, 0, "它该是被 INT 正常收走的")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_进程刚好已经退出时不留假痕迹(self):
        # 信号没送出去就不该说「它被打断了」——假痕迹会让判据把一轮正常失败
        # 说成「接着 resume 即可」
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.assertFalse(ca.pid_alive(dead.pid),
                         "前提不成立：陪练进程还活着，这条测的就不是「已退出」")
        ca.interrupt_codex(dead.pid, self.log)
        self.assertEqual(self.log.read_text(), "")

    def test_只发INT绝不发TERM(self):
        # SIGTERM 会让 thread 永久锁死，之后 resume 永远报 thread-store conflict，
        # 等多久都不释放，上下文全丢
        with mock.patch.object(ca.os, "kill") as k:
            ca.interrupt_codex(4242, self.log)
        k.assert_called_once_with(4242, signal.SIGINT)

    def test_刻意不给返回值(self):
        """「它本来就没在跑」由调用方在**调用之前**用 find_codex_pid 判，
        那才是判它的地方。给个 bool 出来，就多出一个「谁检查」的滥用面。
        """
        with mock.patch.object(ca.os, "kill"):
            self.assertIsNone(ca.interrupt_codex(4242, self.log))


class TestEveryInterruptPathLeavesAMark(_HomeSandbox):
    """三条打断路径必须都留痕。漏一条就会出现「stop 说可 resume、status 说 failed」。

    forward_as_sigint 那条由 TestSignalSafetyRealProcesses 用真信号钉着
    （它断言日志里出现 INTERRUPT_MARK），这里补 stop 这条——正是漏掉的那条。
    """

    def test_stop这条路也留痕(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t"))
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

改一条已有的：`TestSignalSafety.test_包装器收到SIGTERM时向codex转发的是SIGINT`。
`forward_as_sigint` 从 `proc.send_signal(SIGINT)` 改成走 `interrupt_codex(proc.pid, …)`（内部是 `os.kill`），断言跟着换：

```python
    def test_包装器收到SIGTERM时向codex转发的是SIGINT(self):
        with mock.patch.object(ca.subprocess, "Popen") as popen, \
             mock.patch.object(ca.signal, "signal") as sigsig, \
             mock.patch.object(ca.os, "kill") as kill:
            self._run_once(popen)
            handled = {c.args[0] for c in sigsig.call_args_list}
            self.assertEqual(handled, {signal.SIGTERM, signal.SIGINT, signal.SIGHUP})
            sigsig.call_args_list[0].args[1](signal.SIGTERM, None)
        kill.assert_called_once_with(popen.return_value.pid, signal.SIGINT)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestInterruptCodex test_codex_agent.TestEveryInterruptPathLeavesAMark -v`

Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'interrupt_codex'`

- [ ] **Step 3: 最小实现**

把 `note_interrupt` 整个删掉，换成：

```python
def interrupt_codex(pid, log_path, cause):
    """发 SIGINT 并在日志留痕。这两件事必须一起发生，所以焊在同一个函数里。

    拆开放就会漏，而且**已经漏过一次**：`cmd_stop` 用裸 `os.kill` 打给 codex，
    而写痕迹的函数只在包装器自己的信号处理器里被调用，于是 stop 这条路上痕迹
    永远不写。2026-09-19 实测三行互相矛盾：stop 打印「上下文保留，可 resume」、
    包装器收尾判 `failed —— 报告缺失或为空＝没正常收尾`（退出码 1）、
    日志里 INTERRUPT_MARK 计数 **0**。而 agent 看到 failed 会从头重跑，
    把保着的上下文和 token 一起扔掉。

    **只发 INT，永不 TERM**：SIGTERM 会让 thread 永久锁死，之后 resume 永远报
    thread-store conflict，等多久都不释放，上下文全丢。
    「INT 之后仍然可以 resume」2026-09-19 在 codex 0.154.0 上真机复验过：
    run → INT → resume 跑通，两轮 session id 完全相同、token 从 3,216 接着涨到
    3,989，追问「被打断前你成功创建了哪几个文件」它自己答得出——恢复的是语义上
    的上下文，不只是一段计费记录。

    **返回值刻意不给。**「它本来就没在跑」这件事由调用方在**调用之前**用
    `find_codex_pid` 判，那才是判它的地方；给个 bool 出来只是多一个「谁检查」
    的滥用面。这里的 ProcessLookupError 只是「刚好在这一瞬退出了」——
    信号没送出去就不该留下假痕迹，所以直接返回。

    留痕用 O_APPEND + 单次 os.write：小写入在 Linux 上是原子的，不会和 tee 循环
    的缓冲写互相撕裂；也刻意不碰那个已经打开的文件对象——信号处理器随时可能插在
    它的 write 中间。写不进去就算了（吞掉 OSError）：INT 已经发出去了，保住
    codex 的上下文优先于留痕。这个降级方向正是「可观测的失效不许拖垮存活」。
    """
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (INTERRUPT_MARK + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass
```

`forward_as_sigint` 缩成一行：

```python
        def forward_as_sigint(signum, frame):
            # 无论包装器被谁、用什么信号停，codex 收到的永远是 INT，上下文永远可 resume，
            # 而且日志里一定留下痕迹——之后跑判据的人（包括另一个进程里的 status）
            # 才知道这轮该 resume 而不是重跑。
            # 刻意不在这里退出：让 tee 循环自然跑完，判据照样出、完成通知照样带结论。
            interrupt_codex(proc.pid, _log_path(home, task))
```

`cmd_stop` 里那一行：

```python
    interrupt_codex(pid, _log_path(home, args.task), "stop")
    print(f"已向 {args.task} (pid={pid}) 发 SIGINT，上下文保留，可 resume")
```

（`pid is None` 的分支原样留在上面，那正是「调用之前判」的地方。）

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`

Expected: PASS，**138 个全绿**（133 + 新 5：`TestInterruptCodex` 4 条 + `TestEveryInterruptPathLeavesAMark` 1 条。初稿写的 135／新 4 漏了后者，Task 4~7 的预期数跟着一路偏小 1，已全部改正）。

- [ ] **Step 5: 突变复验**

1. `cmd_stop` 改回裸 `os.kill(pid, signal.SIGINT)`（不调 `interrupt_codex`）
   → `test_stop这条路也留痕` 必须红
2. `forward_as_sigint` 改回 `proc.send_signal(signal.SIGINT)`（不留痕）
   → `TestSignalSafetyRealProcesses.test_向包装器的进程组发TERM_codex只会收到INT` 必须红（它断言日志里出现 `INTERRUPT_MARK`）
3. `interrupt_codex` 把 `except ProcessLookupError: return` 改成 `pass`（即照样留痕）
   → `test_进程刚好已经退出时不留假痕迹` 必须红

还原。

- [ ] **Step 6: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "fix: 发 INT 与留痕焊成 interrupt_codex，note_interrupt 并进去

三条路（信号转发、stop、新命令）全部走它。stop 那条此前漏了：实测它刚打印
「可 resume」，判据就说 failed，日志里 INTERRUPT_MARK 计数 0。
刻意不给返回值——「它本来就没在跑」由调用方在调用之前用 find_codex_pid 判。"
```

---

### Task 4: `wait_for_exit()` —— 只轮询，绝不加火力

**Files:** Modify `codex_agent.py` · Modify `test_codex_agent.py`

**Interfaces:**
- Consumes: `find_codex_pid`
- Produces:
  - `wait_for_exit(report_path: pathlib.Path, timeout: float, poll_interval: float) -> bool`
    —— 顺手把 `find_codex_pid` 里的 `needle` 改成 `str(report_path).encode()`，
    全模块就只剩「传 Path」一种传法（`wait_for_exit` 本来是唯一收 `str` 的那个）
  - 模块级常量 `INTERRUPT_EXIT_TIMEOUT = 60`、`INTERRUPT_POLL_INTERVAL = 0.2`
- `codex_agent.py` 文件头加 `import time`

- [ ] **Step 1: 写失败的测试**

```python
class TestWaitForExit(unittest.TestCase):
    def test_进程退出后返回True(self):
        with mock.patch.object(ca, "find_codex_pid", side_effect=[4242, 4242, None]):
            self.assertTrue(ca.wait_for_exit("/x/reports/t.md", 5, 0.01))

    def test_一直不退则超时返回False(self):
        with mock.patch.object(ca, "find_codex_pid", return_value=4242):
            self.assertFalse(ca.wait_for_exit("/x/reports/t.md", 0.05, 0.01))

    def test_等待期间绝不发任何信号(self):
        # 超时的正确处置是告诉调用方稍后再来，不是加大火力。
        # 升级到 SIGTERM 会让会话永久锁死，而那一步不可逆。
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

    def test_两个常量的绝对值_并且余量对得上实测(self):
        """常量必须是**模块级**的，不是函数默认参数：测试要把它压到 0.3 秒，
        而仓库规范本来就不许用默认缺省值。

        60 秒的实测锚点（codex 0.154.0、effort low、sleep 工具调用执行中被打断）：
        INT → PID 消失分别是 1.854 秒和 0.964 秒。这里把「30~60 倍余量」这个
        **理由**也钉住——只钉 60 这个数字的话，下一个人把实测值改了没人拦。
        """
        self.assertEqual(ca.INTERRUPT_EXIT_TIMEOUT, 60)
        self.assertEqual(ca.INTERRUPT_POLL_INTERVAL, 0.2)
        self.assertGreaterEqual(ca.INTERRUPT_EXIT_TIMEOUT, 1.854 * 30)
        self.assertLess(ca.INTERRUPT_POLL_INTERVAL, 0.964,
                        "轮询间隔比实测最快的退出还长，等于把等待时间凭空拉长一轮")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestWaitForExit -v`

Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'wait_for_exit'`

- [ ] **Step 3: 最小实现**

文件头 `import subprocess` 之后加 `import time`（**并进文件头，不许写在函数里**）。

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`

Expected: PASS，**143 个全绿**（138 + 新 5）。

- [ ] **Step 5: 突变复验**

1. `wait_for_exit` 里在超时那一支加 `os.kill(find_codex_pid(report_path), signal.SIGTERM)`
   → `test_等待期间绝不发任何信号` 必须红
2. 把 `INTERRUPT_EXIT_TIMEOUT` 改成 5
   → `test_两个常量的绝对值_并且余量对得上实测` 必须红（`5 < 1.854 * 30`）
3. 把常量改成函数默认参数 `def wait_for_exit(report_path, timeout=60, poll_interval=0.2)`
   → `test_两个常量的绝对值` 必须红（模块级常量没了）

还原。

- [ ] **Step 6: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: wait_for_exit 只轮询不加火力，超时也绝不升级到 TERM

把那个每个调用方都要现编一遍的重试循环收进代码——护栏只会拒绝，不会替你等，
这是 interrupt-and-resume 唯一独有的收益。
60 秒的锚点是实测 INT→退出 1.854s / 0.964s 的 30~60 倍余量。"
```

---

### Task 5: 四道闸 + `_resume_round` + `cmd_interrupt_and_resume`

**Files:** Modify `codex_agent.py` · Modify `test_codex_agent.py`

**Interfaces:**
- Consumes: Task 1~4 的全部产出、`find_meta`、`ensure_isolation`、`run_codex`、`judge`、`read_round`、`read_last_round`
- Produces（确切签名）：
  - `check_can_resume(task: str, meta: dict, brief_path: str) -> None` —— 只拒绝，无副作用
  - `_resume_round(kind: str, home: pathlib.Path, meta: dict, task: str, brief_path: str, effort: str) -> int`
  - `cmd_interrupt_and_resume(args) -> int`
- `cmd_resume` 收缩成「查任务 + 拦住还在跑的 + 调 `_resume_round`」

**执行顺序是硬约束**（不是排版顺序）：

```
1. 任务存在？           不存在 → 拒绝
2. 元数据有 session id？ 没有   → 拒绝
3. 工作目录还在？        不在   → 拒绝
4. --brief 是文件？      不是   → 拒绝
   ────────── 以上四道闸全过，才允许动手 ──────────
5. 查真实 PID
   ├ 没在跑 → 打印「本来就没在跑，直接续跑」
   └ 在跑   → 本轮日志已有 INTERRUPT_MARK？
              ├ 有 → 只等，不再发信号
              └ 无 → interrupt_codex → wait_for_exit
6. 续跑
```

- [ ] **Step 1: 写失败的测试**

```python
class TestInterruptAndResumeOrder(_HomeSandbox):
    """所有拒绝都必须发生在**发信号之前**。

    INT 发出去就收不回来。先打断、再发现没 session id，那一轮白毁**且拿不回来**
    （没 session id 就没法 resume）。这个窗口真实可达——session_id 要等 codex
    第一块输出才写进元数据，实测父进程 4.06 秒才看到 banner，而任务刚起那几秒
    正是最可能被打断的时候（刚发现 brief 写错）。
    """

    def setUp(self):
        super().setUp()
        self.workdir = self.home / "repo"
        self.workdir.mkdir()
        self.brief = self.home / "msg.md"
        self.brief.write_text("顺便把 X 也改了")
        self.d = ca.ensure_isolation("default")
        ca.write_meta(self.d, "t", _full_meta("t", session_id="s1", dir=str(self.workdir)))
        ca._log_path(self.d, "t").write_text(
            ca.round_separator("run", "t", "2026-09-19T00:00:00") + "\n")

    def _args(self, task="t", brief=None):
        """这一类测的是**命令体的执行顺序**，不是参数表——参数表归 Task 6，
        那里用真 parser 从命令行一路验下来。所以这里直接搭 Namespace：
        三个字段逐个显式写出，不走默认值。
        """
        return argparse.Namespace(
            task=task, brief=str(self.brief if brief is None else brief), effort="low")

    def test_在跑时顺序是先打断再确认退出再续跑(self):
        order = []
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "interrupt_codex",
                               side_effect=lambda *a: order.append("打断")), \
             mock.patch.object(ca, "wait_for_exit",
                               side_effect=lambda *a: order.append("等退出") or True), \
             mock.patch.object(ca, "_resume_round",
                               side_effect=lambda *a: order.append("续跑") or 0):
            self.assertEqual(ca.cmd_interrupt_and_resume(self._args()), 0)
        self.assertEqual(order, ["打断", "等退出", "续跑"], "顺序反了就会撞写锁")

    def test_没在跑时不发信号直接续跑(self):
        # 调用方无法可靠知道自己在哪种情况——查完到动手之间任务可能刚好跑完
        with mock.patch.object(ca, "find_codex_pid", return_value=None), \
             mock.patch.object(ca, "interrupt_codex") as ic, \
             mock.patch.object(ca, "_resume_round", return_value=0):
            self.assertEqual(ca.cmd_interrupt_and_resume(self._args()), 0)
        ic.assert_not_called()

    def test_没有session_id时绝不发信号_那一轮白毁且拿不回来(self):
        # 三条闸测试都要 mock 掉 wait_for_exit。不 mock 的话，闸序一坏就会掉进
        # 真的 60 秒等待：实测「把 check_can_resume 挪到发信号之后」这个突变要
        # **180.3 秒**才红，而且报的是「收到 INT 后 60 秒还没退出」——
        # 闸序坏了，报的却是等超时，指错了方向。mock 之后同一突变 2.1 秒变红，
        # 报错是 `Expected 'interrupt_codex' to not have been called`，正中要害。
        ca.write_meta(self.d, "t2", _full_meta("t2", dir=str(self.workdir)))
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "wait_for_exit", return_value=True), \
             mock.patch.object(ca, "interrupt_codex") as ic, \
             mock.patch.object(ca.os, "kill") as k:
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_interrupt_and_resume(self._args("t2"))
        self.assertIn("session id", cm.exception.message)
        ic.assert_not_called()
        k.assert_not_called()

    def test_工作目录没了时绝不发信号(self):
        ca.write_meta(self.d, "t3", _full_meta("t3", session_id="s3",
                                               dir=str(self.home / "已经删了的worktree")))
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "wait_for_exit", return_value=True), \
             mock.patch.object(ca, "interrupt_codex") as ic, \
             mock.patch.object(ca.os, "kill") as k:
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_interrupt_and_resume(self._args("t3"))
        self.assertIn("不在了", cm.exception.message)
        ic.assert_not_called()
        k.assert_not_called()

    def test_brief不是文件时绝不发信号(self):
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "wait_for_exit", return_value=True), \
             mock.patch.object(ca, "interrupt_codex") as ic, \
             mock.patch.object(ca.os, "kill") as k:
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_interrupt_and_resume(self._args(brief=self.home / "根本没有这个文件.md"))
        self.assertIn("不是文件", cm.exception.message)
        ic.assert_not_called()
        k.assert_not_called()

    def test_任务不存在时绝不发信号(self):
        with mock.patch.object(ca, "interrupt_codex") as ic, \
             mock.patch.object(ca.os, "kill") as k:
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_interrupt_and_resume(self._args("no-such-task"))
        self.assertIn("没有这个任务", cm.exception.message)
        ic.assert_not_called()
        k.assert_not_called()

    def test_等不到退出就拒绝续跑且绝不升级信号(self):
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "interrupt_codex"), \
             mock.patch.object(ca, "wait_for_exit", return_value=False), \
             mock.patch.object(ca, "_resume_round") as rw:
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_interrupt_and_resume(self._args())
        rw.assert_not_called()
        self.assertEqual(cm.exception.code, 2)      # 绝对值：护栏拒绝就是 2
        self.assertIn("稍后", cm.exception.message)

    def test_本轮已有打断痕迹时只等不发第二发INT(self):
        """超时的处置是「稍后重试」，而重试就是再跑一遍这条命令 → 又一次
        interrupt_codex → 第二发 INT。很多 CLI 把第二发 Ctrl-C 当强退；
        codex 是不是这样**完全没验过**。如果是，就可能走成不干净退出 →
        写锁不释放 → 上下文全丢，正是本命令要防的事。
        「永不升级信号」在单次调用内成立，被重试路径绕过去了。
        """
        with open(ca._log_path(self.d, "t"), "a") as f:
            f.write(ca.INTERRUPT_MARK + "\n")
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "interrupt_codex") as ic, \
             mock.patch.object(ca.os, "kill") as k, \
             mock.patch.object(ca, "wait_for_exit", return_value=True) as w, \
             mock.patch.object(ca, "_resume_round", return_value=0):
            self.assertEqual(ca.cmd_interrupt_and_resume(self._args()), 0)
        ic.assert_not_called()
        # 「零调用」要钉 os.kill 本身，不能只钉 interrupt_codex：在那一支里加一行
        # 裸 os.kill 也会红，但红的原因是 ProcessLookupError（4242 不存在）这个
        # **巧合**——pid 若恰好存在就是绿的。同一个类里四条闸测试都钉了 os.kill。
        k.assert_not_called()
        # **等的是谁**也要钉死：把报告路径传成日志路径，find_codex_pid 永远找不到，
        # wait_for_exit 秒返 True、等待整个被跳过、直接续跑撞写锁——而这正是
        # 这条命令唯一独有的收益。实跑确认：不钉参数的话这个突变 156 条全绿。
        w.assert_called_once_with(ca._report_path(self.d, "t"),
                                  ca.INTERRUPT_EXIT_TIMEOUT, ca.INTERRUPT_POLL_INTERVAL)

    def test_上一轮的打断痕迹不算数_本轮还是要发INT(self):
        # 反面钉一道：判据必须只看**本轮**。看全文的话，一个被打断过的任务
        # 之后永远发不出 INT 了。
        log = ca._log_path(self.d, "t")
        log.write_text(ca.round_separator("run", "t", "2026-09-19T00:00:00") + "\n"
                       + ca.INTERRUPT_MARK + "\n"
                       + ca.round_separator("resume", "t", "2026-09-19T00:00:01") + "\n干净\n")
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "interrupt_codex") as ic, \
             mock.patch.object(ca, "wait_for_exit", return_value=True), \
             mock.patch.object(ca, "_resume_round", return_value=0):
            ca.cmd_interrupt_and_resume(self._args())
        # **打给谁**也要钉：传成报告路径的话，痕迹写进**报告** → judge 看到非空
        # 报告、无错误行 → 判 success、退出码 0。不是崩，是静默说谎。
        # 实跑确认：只钉 assert_called_once() 的话这个突变 156 条全绿。
        ic.assert_called_once_with(4242, ca._log_path(self.d, "t"))

    def test_日志分隔符写的是interrupt_and_resume_而不是resume(self):
        # 日志要看得出这一轮是被插话打断后续上的
        seen = {}
        with mock.patch.object(ca, "find_codex_pid", return_value=None), \
             mock.patch.object(ca, "run_codex",
                               side_effect=lambda kind, *a, **k: seen.update(kind=kind) or 0), \
             mock.patch.object(ca, "judge", return_value=ca.Verdict("success", "ok", [])):
            ca.cmd_interrupt_and_resume(self._args())
        self.assertEqual(seen["kind"], "interrupt-and-resume")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestInterruptAndResumeOrder -v`

Expected: FAIL，`AttributeError: <module 'codex_agent'> does not have the attribute '_resume_round'`
（`mock.patch.object` 在属性不存在时当场抛；`interrupt_codex` / `wait_for_exit` 已经在 Task 3/4 就位了）

- [ ] **Step 3: 最小实现**

先抽出三道闸——**这是「拒绝必须在发信号之前」这条硬约束的结构化身**：

```python
def check_can_resume(task, meta, brief_path):
    """续跑的三道闸。**只拒绝，不产生任何副作用**，所以可以在发信号之前先跑一遍。

    抽成独立函数，是因为 `interrupt-and-resume` 必须把**全部**拒绝跑在发信号
    之前：INT 发出去就收不回来，先打断、再发现没 session id，那一轮白毁**且拿
    不回来**（没 session id 就没法 resume）。这个窗口真实可达——session_id 要等
    codex 第一块输出才写进元数据，实测父进程 4.06 秒才看到 banner，而任务刚起
    那几秒正是最可能被打断的时候（刚发现 brief 写错）。

    `_resume_round` 自己也调它：闸留在续跑动作里，才没有一条绕过去的后门。
    两次调用是刻意的，纯拒绝、无副作用，跑两遍不花钱。
    """
    if not meta["session_id"]:
        reject(f"任务 {task} 没有记到 session id，无法 resume，只能新起一个任务")
    workdir = pathlib.Path(meta["dir"])
    if not workdir.is_dir():
        reject(f"任务 {task} 的工作目录 {workdir} 不在了（worktree 被删？）。"
               f"codex 会以 os error 2 当场崩，所以这里直接拒。")
    if not pathlib.Path(brief_path).expanduser().is_file():
        reject(f"--brief {brief_path} 不是文件（brief 只收文件路径，避开引号地狱）")
```

再把 `cmd_resume` 的后半段抽成两条路共用的续跑动作：

```python
def _resume_round(kind, home, meta, task, brief_path, effort):
    """两条路共用的续跑动作：`resume` 和 `interrupt-and-resume`。

    名字是 `_resume_round` 不是 `_resume_with`：`with` 没说清 with 什么，
    而它做的事就是「跑完一轮续跑」（仓库规范第 2 条：不清晰的词换成清晰的词组）。

    它只管「已经确定停了之后怎么续」，**不判断该不该停**——`cmd_resume` 在调它
    之前拒绝还在跑的任务，`cmd_interrupt_and_resume` 在调它之前把它打断并确认
    退出。这条边界是刻意的：要不要停是调用方的判断，怎么续是工具的事。

    `kind` 进日志分隔符，所以日志里看得出这一轮是被插话打断后续上的。
    判据用 `run_codex` 回传的偏移，**不用 read_last_round**：本轮的拥有者手里
    有事实，用最后一轮就是把事实换回推测（那正是 A-1 修掉的整类 bug）。
    """
    check_can_resume(task, meta, brief_path)
    ensure_isolation(meta["account"])
    brief = prepend_skill_guard(pathlib.Path(brief_path).expanduser().read_text())
    # 元数据描述的是**最后一次调用**：effort 和开跑时间都刷新。
    # 完整的轮次历史不在这里，在日志的分隔符里（每轮一行，带时间戳）。
    meta["effort"] = effort
    meta["started_at"] = _now_iso()
    start_offset = run_codex(kind, home, task, meta,
                             lambda r: build_resume_argv(meta["dir"], meta["session_id"],
                                                         effort, r, brief))
    verdict = judge(_report_path(home, task),
                    read_round(_log_path(home, task), start_offset), None)
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
    if find_codex_pid(str(_report_path(home, args.task))) is not None:
        reject(f"任务 {args.task} 还在跑，resume 会撞上它自己的写锁。"
               f"等它结束，或用 `codex-agent interrupt-and-resume {args.task}`。")
    return _resume_round("resume", home, meta, args.task, args.brief, args.effort)


def cmd_interrupt_and_resume(args):
    """打断当前轮 + 确认它真的退出了 + 用新消息续跑，三件事不可分。

    它唯一独有的收益：**护栏只会拒绝，不会替你等。** `cmd_resume` 早就拦住了
    「对还在跑的会话 resume」（实测 stop 之后 0.164 秒 resume，拿到的是干净的
    exit 2 拒绝，不是 thread-store conflict），但调用方拿到 exit 2 之后得自己
    写重试循环——间隔多少、上界多少、超时了怎么办，全是软约定，每个调用方现编
    一遍，编错了没人告诉他。收走这个循环就是这条命令存在的全部理由。

    **要不要为此打断，仍然是调用方的判断**：那要知道「这条信息值多少」和「在途
    工作损失多少」，后者在 codex 里根本不可观测。命令名把代价写在脸上，
    工具不替谁做这个决定。

    下面的顺序是**硬约束**，不是排版顺序：四道闸全部走完才允许发信号。
    """
    # ────── 四道闸 ──────
    home, meta = find_meta(args.task)                      # 1. 任务存在？
    if meta is None:
        reject(f"没有这个任务：{args.task}")
    check_can_resume(args.task, meta, args.brief)          # 2/3/4. session id / 目录 / brief
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
        if INTERRUPT_MARK in read_last_round(log):
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
    return _resume_round("interrupt-and-resume", home, meta, args.task, args.brief, args.effort)
```

- [ ] **Step 4: 跑测试确认通过**

本任务**不碰 `build_parser`** ——命令行契约整个归 Task 6，那里从命令行一路验下来。
这里交付的是命令体：四道闸的顺序、重试不发第二发 INT、两条路共用的续跑动作。

Run: `python3 -m unittest test_codex_agent -v`

Expected: PASS，**153 个全绿**（143 + 新 10）。此刻 `codex-agent interrupt-and-resume`
在命令行上**还是不存在的**，这是对的：Task 6 才把它接上去。

- [ ] **Step 5: 突变复验**（三个，全部是承重约束）

1. 把 `check_can_resume(args.task, meta, args.brief)` 挪到 `interrupt_codex` **之后**
   → `test_没有session_id时绝不发信号`、`test_工作目录没了时绝不发信号`、`test_brief不是文件时绝不发信号` 三条必须都红
2. 把 `if INTERRUPT_MARK in read_last_round(log)` 那一支整个删掉（无条件发信号）
   → `test_本轮已有打断痕迹时只等不发第二发INT` 必须红
3. 把 `read_last_round(log)` 换成 `log.read_text()`（看全文而不是本轮）
   → `test_上一轮的打断痕迹不算数_本轮还是要发INT` 必须红
4. `interrupt_codex(pid, log, …)` 改成 `interrupt_codex(pid, report, …)`（留痕写错文件）
   → `test_上一轮的打断痕迹不算数_本轮还是要发INT` 必须红。
   **这条曾经三条全绿**：痕迹写进报告 → judge 看到非空报告、无错误行 → 判
   `success`、退出码 **0**。不是崩，是静默说谎。
5. `wait_for_exit(report, …)` 改成 `wait_for_exit(log, …)`（等错了文件）
   → `test_本轮已有打断痕迹时只等不发第二发INT` 必须红。
   **这条也曾经全绿**：`find_codex_pid` 永远找不到 → 秒返 True →
   等待整个被跳过 → 直接续跑撞写锁，而等待正是这条命令唯一独有的收益。
6. 在「只等不发」那一支里加一行裸 `os.kill(pid, signal.SIGINT)`
   → `test_本轮已有打断痕迹时只等不发第二发INT` 必须红，**且红在 `k.assert_not_called()`**，
   不是红在 ProcessLookupError（那是 4242 不存在的巧合，pid 恰好存在就绿了）

还原。

- [ ] **Step 6: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: interrupt-and-resume——四道闸全过才允许发信号

check_can_resume 抽出来，是「所有拒绝都在发信号之前」这条硬约束的结构化身：
INT 发出去收不回来，先打断再发现没 session id，那一轮白毁且拿不回来。
重试路径不许发第二发 INT：本轮日志已有痕迹就只等不发，用已有的痕迹判，
不加新实体。"
```

---

### Task 6: CLI 参数表与 `resume` 逐条一致

**Files:** Modify `codex_agent.py` · Modify `test_codex_agent.py`

**Interfaces:**
- Consumes: `build_parser`、`task_name`、`EFFORTS`、Task 5 的 `cmd_interrupt_and_resume`
- Produces: 子命令 `interrupt-and-resume` 的完整参数契约 —— `task` 走 `type=task_name`、`--brief` 必填、`--effort` 必填且 `choices=EFFORTS`、不收 `--account`、六个旋钮逐个被拒
- Task 5 刻意没碰 `build_parser`：命令体和命令行契约是两件事，各自有一个真的红→绿循环

- [ ] **Step 1: 写失败的测试**

```python
class TestInterruptAndResumeParser(_HomeSandbox):
    """参数规则与 resume **逐条一致**：任务名走 type=task_name、--brief 只收文件
    路径、--effort 必填无默认、不收 --account。同一个工具里 prompt 只有一种传法。
    """

    def test_与resume同一张参数表_少一个都不收(self):
        parser = ca.build_parser()
        for missing in ["--brief", "--effort"]:
            argv = ["interrupt-and-resume", "t", "--brief", "b.md", "--effort", "low"]
            i = argv.index(missing)
            del argv[i:i + 2]
            with self.subTest(missing=missing), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_effort五档正反都验(self):
        parser = ca.build_parser()
        for e in ca.EFFORTS:
            with self.subTest(effort=e):
                args = parser.parse_args(["interrupt-and-resume", "t",
                                          "--brief", "b.md", "--effort", e])
                self.assertEqual(args.effort, e)
        for bad in ("中等", "ultra", "minimal"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parser.parse_args(["interrupt-and-resume", "t", "--brief", "b.md",
                                   "--effort", bad])

    def test_不收account_账号是查出来的(self):
        with self.assertRaises(SystemExit):
            ca.build_parser().parse_args(["interrupt-and-resume", "t", "--brief", "b.md",
                                          "--effort", "low", "--account", "default"])

    def test_不提供任何旋钮_没有第二种正确行为(self):
        # 每个旋钮都是一个让调用方做错的机会：
        #   --now/--wait   等不等不是选项，不等就会撞写锁
        #   --force        没有「强行续跑」这种正确行为
        #   --timeout      上界是实测锚定的，调它只会把自己挂死或误杀正常收尾
        #   --message      prompt 只有一种传法，就是 --brief 文件
        #   --account      账号从元数据查出来，不可能指错
        parser = ca.build_parser()
        for bad in ["--now", "--wait", "--force", "--timeout", "--message", "--account"]:
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parser.parse_args(["interrupt-and-resume", "t", "--brief", "b.md",
                                   "--effort", "low", bad, "x"])

    def test_子命令接到的确实是这条命令的实现(self):
        # Task 5 的测试是直接拿 Namespace 调命令体的，接错了函数它测不出来。
        # 这里是命令行到实现之间唯一那根线。
        args = ca.build_parser().parse_args(
            ["interrupt-and-resume", "t", "--brief", "b.md", "--effort", "low"])
        self.assertIs(args.func, ca.cmd_interrupt_and_resume)
        self.assertEqual((args.task, args.brief, args.effort), ("t", "b.md", "low"))
```

再往 `TestExitCodeContract` 补新命令的**四态绝对值**。`run` 和 `resume` 在那里都有
0/1/3，新命令一条都没有——Task 5 的测试全把 `_resume_round` 或 `judge` mock 掉了，
接线错了它们一个都测不出来。这四条走**真实** `_resume_round`：

```python
    def _interrupt_and_resume(self, report_text, log_extra=""):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t", session_id="s1", dir=str(self.workdir)))
        args = ca.build_parser().parse_args(
            ["interrupt-and-resume", "t", "--brief", str(self.brief), "--effort", "low"])
        # 没在跑：这条路不发信号，直接续跑——验的是「续跑那一轮的判据结论就是退出码」
        with _no_codex() as popen, mock.patch.object(ca, "find_codex_pid", return_value=None):
            popen.side_effect = self._spawner(d, report_text, log_extra)
            return ca.cmd_interrupt_and_resume(args)

    def test_interrupt_and_resume_正常收尾退出0(self):
        self.assertEqual(self._interrupt_and_resume("干完了"), 0)

    def test_interrupt_and_resume_没留下报告退出1(self):
        self.assertEqual(self._interrupt_and_resume(None), 1)

    def test_interrupt_and_resume_报告在但有未分类错误退出3(self):
        self.assertEqual(self._interrupt_and_resume("干完了", self.UNCLASSIFIED), 3)

    def test_interrupt_and_resume_续跑那轮又被打断退出5(self):
        self.assertEqual(self._interrupt_and_resume(None, ca.INTERRUPT_MARK), 5)
```

同时改一条已有的：`TestParser.test_任务名校验挂在四个子命令上_结构上绕不过` 改名成
`test_任务名校验挂在五个子命令上_结构上绕不过`，argv 列表里加一条：

```python
                     ["interrupt-and-resume", "a|b", "--brief", "b.md", "--effort", "low"],
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestInterruptAndResumeParser test_codex_agent.TestParser -v`

Expected: FAIL，`argument cmd: invalid choice: 'interrupt-and-resume'`
—— Task 5 刻意没碰 `build_parser`，这个子命令在命令行上还不存在。

- [ ] **Step 3: 最小实现**

`build_parser` 里 `resume` 之后加：

```python
    # 名字刻意长而直白：它会**截断当前轮**，这个代价必须写在脸上。
    # 仓库规范第 2 条说「不清晰的单词全部换为简单清晰的词组」——
    # `interject` 是个不清晰的单词，`interrupt-and-resume` 是个清晰的词组。
    # 参数表与 resume 逐条一致：同一个工具里 prompt 只有一种传法。
    # help= 进父 parser 的子命令列表，description= 进它自己的 --help。
    # 只传 help= 的话，`interrupt-and-resume --help` 里看不到「会截断当前轮」——
    # 而那正是这条命令最该被看见的代价。
    _IAR_HELP = "打断当前轮并用新消息续跑（会截断当前轮，上下文保留）"
    j = sub.add_parser("interrupt-and-resume", help=_IAR_HELP, description=_IAR_HELP)
    j.add_argument("task", type=task_name)
    j.add_argument("--brief", required=True)
    j.add_argument("--effort", required=True, choices=EFFORTS)
    j.set_defaults(func=cmd_interrupt_and_resume)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`

Expected: PASS，**163 个全绿**（153 + `TestInterruptAndResumeParser` 5 条 + `TestExitCodeContract` 里新命令的四态绝对值 4 条 + `TestWrapperSpeaksImmediately` 1 条）。

- [ ] **Step 5: 自检 CLI**

```bash
python3 codex_agent.py --help
python3 codex_agent.py interrupt-and-resume --help
```

Expected: 五条子命令都在；`codex-agent --help` 的子命令列表里看得到「会截断当前轮」；
新命令自己的 `--help` 里也看得到——**这要 `description=`**，`help=` 只出现在父 parser 的
列表里（实测 `interrupt-and-resume --help | grep -c 截断` = 0）。两个都传。
它只有 `task` / `--brief` / `--effort` 三个参数。

- [ ] **Step 6: 突变复验**

把 `j.add_argument("--effort", required=True, choices=EFFORTS)` 改成 `required=False, default="low"`
→ `test_与resume同一张参数表_少一个都不收` 必须红（这正是仓库规范「不要默认缺省值」要防的）。还原。

- [ ] **Step 7: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: interrupt-and-resume 的参数表与 resume 逐条一致

任务名走 type=task_name、--brief 只收文件、--effort 必填无默认、不收 --account。
六个旋钮逐个被拒：没有第二种正确行为。"
```

---

### Task 7: 文档同步（`SKILL.md` + 测试模块 docstring）

`SKILL.md` 只写**人／模型才能决定的那件事**。信号怎么发、窗口怎么等、痕迹怎么留、边界怎么划——都是代码的事，写进去就是第二个家。这条判据本身有测试守着（`TestSkillDocDoesNotRepeatCode`）。

同时把 `test_codex_agent.py` 的模块 docstring 同步过来：它是这个项目的**维护者入口**，里面那张「承重约束」清单现在有两条已经说的不是同一件事了。改了代码不同步它，下一个维护者读到的就是假话。

**Files:** Modify `SKILL.md` · Modify `test_codex_agent.py`

**Interfaces:**
- Consumes: `EXIT`、`EFFORTS`、`TestSkillDocDoesNotRepeatCode.OWNED_BY_CODE`
- Produces: 文档里出现第四条命令与第五态；泄漏数仍为 0；模块 docstring 的承重约束清单与代码一致

- [ ] **Step 1: 先把文档测试改严（它现在还不该绿）**

在 `TestSkillDocDoesNotRepeatCode.test_文档仍然保留代码替不了的那部分` 里改三处：

```python
        # 五条命令都得在，否则调用方不知道有这些能力
        for cmd in ("run", "status", "resume", "stop", "interrupt-and-resume"):
            with self.subTest(cmd=cmd):
                self.assertIn(f"codex-agent {cmd}", skill)
        ...
        for code in ("0", "1", "3", "4", "130"):        # 退出码是对外契约
            with self.subTest(code=code):
                self.assertIn(f"`{code}`", skill)
        # 「要不要为此打断」是判断力，代码替不了：它要知道这条信息值多少、
        # 在途工作损失多少，后者在 codex 里根本不可观测
        self.assertIn("值不值", skill)
```

Run: `python3 -m unittest test_codex_agent.TestSkillDocDoesNotRepeatCode -v`

Expected: FAIL，`'codex-agent interrupt-and-resume' not found in ...`

- [ ] **Step 2: 改 `SKILL.md` 三处**

1. 标题「另外三条命令」→「另外四条命令」，代码块加一行：

```bash
codex-agent interrupt-and-resume <任务名> --brief msg.md --effort low
```

2. 「没有收件箱」那段改成（**只讲判断，不讲机制**）：

> `codex exec` **没有收件箱**——给正在跑的那一轮塞消息是做不到的。要给它新信息，
> 只有 `interrupt-and-resume` 一条路，而它＝**打断当前轮**。
>
> **要不要为此打断，看这条信息值不值。** 工具替不了这个判断：它要知道「这条信息
> 值多少」和「在途工作损失多少」，后者在 codex 里根本不可观测。
> 已做的部分**留在上下文里不会白做**（实测：打断后续跑，追问它被打断前成功建了
> 哪几个文件，它自己答得出，磁盘上也确实只有那几个），但当前这一轮的收尾会没有。

3. 退出码那一行加第五态和「看数字不看词」：

> 退出码 | `0` success、`1` failed、`3` suspect（干完了，但本轮日志有未分类的 codex 错误，要人看一眼）、`4` running、`130` interrupted（被打断，**接着续跑即可，不要重跑**——130 就是 Ctrl-C 那个既成约定，脚本作者不读本文档也认得）、`2` 参数写错或被护栏拒绝。**看数字，不看词**：完成通知对任何非零码都写 `failed with exit code N`，「failed」这个词消不掉，能区分的只有那个数字

- [ ] **Step 3: 跑泄漏测试确认没写进机制**

Run: `python3 -m unittest test_codex_agent.TestSkillDocDoesNotRepeatCode -v`

Expected: PASS。新写的文字里不许出现 `OWNED_BY_CODE` 的任何一个模式——逐条自查：
`--cd`、`/dev/null|DEVNULL`、`/proc|pgrep|pkill`、`kill -INT|SIGTERM|SIGINT`、`mkdir -p`、
`--color`、`CODEX_HOME|CODEX_SQLITE_HOME`、`--sandbox|sandbox_mode`、
`--disable|project_doc_max_bytes|approval_policy`、`session id|session_id`、
`\bexit 1\b|退出码不可信`。若红，是新文字泄漏了机制，删掉那句而不是改测试。

- [ ] **Step 4: 同步测试模块 docstring**

`test_codex_agent.py` 顶上那张「承重约束」清单改两行——改的不是措辞，是它现在说错了的两件事：

```
    开跑前三件事的顺序（落元数据、清旧报告、写轮次分隔符，全在 spawn 之前）
    防陈旧报告（clear_report）、日志追加而非覆盖、**轮次边界由拥有者记下而非推测**
    三种错误形式（用户层 ERROR: / tracing / 顶层 Error:）与良性 target 白名单
    退出码**五态**的绝对值、严重度排序（interrupted 在 running 之后）、元数据字段清单
```

再在「空测试」那一节的三条病例后面补第四条（用户在这次改动里点名的那条）：

```
    4. `assertEqual(ca.judge(Round(report, log_text)).state, "failed")` 只钉状态，不钉
       「这个结论是从哪段日志得出的」。后来的一轮往同一个日志追加分隔符，判据
       被致盲，而这条测试照样绿——回归锁要同时钉住**结论**和**边界**
       （见 TestRoundBoundary：正面钉拥有者的偏移，反面钉「猜边界当场失明」）。
```

- [ ] **Step 5: 全量测试**

Run: `python3 -m unittest test_codex_agent -v`

Expected: PASS，**163 个全绿**（本任务不增减测试数，只把三条断言改严 + 改 docstring）。

- [ ] **Step 6: 突变复验**

把 `SKILL.md` 里那句「已做的部分留在上下文里不会白做」改成写机制的说法
（例如「工具会给它发 SIGINT 再等它退出」）
→ `test_没有一条代码级约束泄漏进文档` 必须红。还原成只讲判断的那一版。

- [ ] **Step 7: 提交**

```bash
git add SKILL.md test_codex_agent.py
git commit -m "docs: SKILL.md 加第四条命令与第五态，只写判断力不写机制

要不要为此打断，要知道这条信息值多少、在途工作损失多少，后者在 codex 里
根本不可观测——这是代码替不了的那部分。
退出码加 130 interrupted，并写明看数字不看词：完成通知对任何非零码都写
failed with exit code N，能区分的只有那个数字。
测试模块 docstring 的承重约束清单一并同步：轮次边界由拥有者记下而非推测、
退出码五态、以及新的第四条空测试病例。"
```

---

### Task 7b: 三条没人看守的承重约束（代码审查 Important-2/3/4）

三条都是**突变存活**——改坏了 163 条全绿。

**① `O_APPEND` 无人看守。** 把 `interrupt_codex` 的 `O_APPEND` 去掉改成从 0 覆盖写，
全绿。后果：痕迹落在 `start_offset` **之前** → `read_round` 看不到 → 判 `failed`
（这个分支存在的理由被静默重新引入）；同时日志头部的分隔符和 session id 被覆盖
→ resume 再也回不来。
**根因是模块 docstring 点名的第 1 类空测试：前提本身不成立。** 三条留痕测试全在
**空日志**上跑，而空文件上覆盖写和追加写结果一模一样。docstring 把「日志追加而非
覆盖」列在「已全部被杀」里，那只覆盖了 `run_codex` 的 `"ab"`，**没覆盖 `interrupt_codex`**。
改法：三条留痕测试的前提改成**非空日志**（头部放分隔符 + `session id:` 行），
断言原有内容还在、痕迹落在头部**之后**。

**② `_say` 是把软约定换了个地方。** 它的 docstring 说「收成一个函数而不是每个
print 加 flush，后者是软约定漏一个就静默错序」——但 `_say` 有**完全相同的弱点，
只是上移了一层**：15 个调用点都得记得用它。实测新加一行裸 `print` 存活、把
「已确认退出」那句改回裸 `print` 也存活。它声称要防的失效完全可达且完全没测。
**治本：在入口把文本层改成行缓冲，裸 `print` 自动正确，`_say` 整个删掉。**

```python
def main():
    # stdout 接管道／文件时文本层默认**块缓冲**，而这条 fd 有两个写者：
    # 本模块的 print 走文本层，run_codex 的 tee 走 sys.stdout.buffer（自己 flush）。
    # 不改成行缓冲，包装器「此刻正在发生什么」的话会排到 codex 整轮输出之后
    # （2026-09-19 端到端实测拿到过这个错序）。
    # 在这里改一次，而不是每个 print 加 flush=True，也不是收一个 _say()——
    # 那两种都是软约定，漏一个就静默错序，而漏一个不会报错。
    sys.stdout.reconfigure(line_buffering=True)
```

`TestWrapperSpeaksImmediately` 的驱动改成走 `main()`，并**新增一条**：驱动里用
**裸 `print`** 也必须当场读得到——那才是真正被钉住的东西。

**③ SKILL.md 退出码表的语义无人看守。** 现在只断言「这个数字以反引号出现过」，
把表改成 `` `1` interrupted `` / `` `130` failed ``（五个数字一个不少、含义全反）→ 全绿。
改法：钉**数字和状态名的配对**，配对表从 `EXIT` 派生，不另写清单。

```python
        for state, code in ca.EXIT.items():
            with self.subTest(state=state):
                self.assertRegex(skill, rf"`{code}`\s*{state}",
                                 f"SKILL.md 里 {code} 没有紧跟着 {state}")
        self.assertRegex(skill, r"`2`.*(参数|护栏)")   # USAGE_ERROR 不在 EXIT 表里
```

---

### Task 8: 端到端真机验证

单测把每条约束都钉住了，但没有一条真的把 codex 叫起来过。这一步验的是「传对了之后，codex 那一层真的照做了没」。

**Files:** 无（验证性任务，只提交一条空提交记录结论）

**Interfaces:**
- Consumes: 全部五条命令
- Produces: 逐条记录的实际输出——打断留痕、被打断那一轮的退出码是 5、上下文真的接上、没在跑时也能用、四道闸挡在发信号之前
- 不在这里验的：**重试不发第二发 INT**（要造出「INT 已发、进程还没退」那个窗口不可控，单测已经用日志痕迹把它钉死了）

**约定**：下面每个 code block 开头都重设 `CA` 和 `D` —— Bash 工具的 shell 变量**不跨调用保留**，只在第一块里定义会让后面几块静默拿到空路径。

- [ ] **Step 1: 造一个会跑一阵子的真任务**

```bash
CA=/home/xy/.claude/skills/codex-agent/.worktree/interrupt-and-resume/codex_agent.py
D=/tmp/iar-smoke-2026-09-19
rm -rf $D && mkdir -p $D && git -C $D init -q
cat > $D/brief.md <<'EOF'
请按顺序做三件事，不要跳过：
1. 创建 s1.txt，内容为一行：s1 done
2. 运行 shell 命令 `sleep 120`（刻意的等待，请真的执行并等它结束）
3. sleep 结束后创建 s2.txt，内容为一行：s2 done
最后一句话说明你创建了哪些文件。
EOF
```

- [ ] **Step 2: 后台起任务**（必须 `Bash(run_in_background: true)`，完成通知里的退出码是这一步的产物）

```bash
CA=/home/xy/.claude/skills/codex-agent/.worktree/interrupt-and-resume/codex_agent.py
D=/tmp/iar-smoke-2026-09-19
python3 $CA run --task iar-smoke-2026-09-19 --dir $D --brief $D/brief.md \
  --effort low --account default
```

- [ ] **Step 3: 确认它真的进了 sleep，再插话**

```bash
CA=/home/xy/.claude/skills/codex-agent/.worktree/interrupt-and-resume/codex_agent.py
D=/tmp/iar-smoke-2026-09-19
# 前提：任务确实在跑，否则这一步测的不是打断。两条都不成立就停下来，别往下走。
python3 $CA status iar-smoke-2026-09-19          # 必须是 running
# `&& echo` 拦不住人：前提不成立只是不打印、脚本照跑，后面测的就不是「打断」了
test -f $D/s1.txt || { echo "前提不成立：s1.txt 还没建出来，这一步测不到打断"; exit 1; }

cat > $D/msg.md <<'EOF'
改一下：不用等 sleep 了，直接创建 s3.txt，内容为一行：interrupted ok
并在收尾自述里说明：被打断前你已经成功创建了哪些文件。
EOF
python3 $CA interrupt-and-resume iar-smoke-2026-09-19 --brief $D/msg.md --effort low
```

Expected（逐条记录实际输出，不许只写「符合预期」）：
- 打印「还在跑（pid=…），已发 SIGINT 并在日志留痕」
- 打印「已确认退出，本轮被提前结束」
- 续跑成功，`$D/s3.txt` 出现
- 收尾自述里 codex **自己说得出被打断前建了 s1.txt** → 上下文真的接上了

- [ ] **Step 4: 被打断那一轮的结论不再说谎**

```bash
L=~/.codex-subagent/logs/iar-smoke-2026-09-19.log
grep -c "本轮被 INT 打断" $L                              # 打断标记 ≥ 1
awk '/interrupt-and-resume/{exit} {print}' $L | tail -5   # 被打断那一轮的尾巴
```

Expected: 打断标记 ≥ 1；**Step 2 那个后台任务的完成通知里的退出码是 130，不是 1**
（这一条是整个改动 A 的验收点：退出码是唯一能到达调用方的通道）。

- [ ] **Step 5: 没在跑时也能用**

```bash
CA=/home/xy/.claude/skills/codex-agent/.worktree/interrupt-and-resume/codex_agent.py
D=/tmp/iar-smoke-2026-09-19
cat > $D/msg2.md <<'EOF'
再创建 s4.txt，内容为一行：second interrupt
EOF
python3 $CA interrupt-and-resume iar-smoke-2026-09-19 --brief $D/msg2.md --effort low
echo "退出码 $?"
```

Expected: 打印「本来就没在跑，直接续跑」，`$D/s4.txt` 出现，退出码 0。

- [ ] **Step 6: 四道闸真的挡在发信号之前**

这一步验的是整个改动 B 里唯一不可逆的那条约束：**拒绝必须发生在发信号之前**。
判据不是「拒绝了没」，而是**拒绝的那一刻日志里的打断标记计数没有变**。

```bash
CA=/home/xy/.claude/skills/codex-agent/.worktree/interrupt-and-resume/codex_agent.py
G=/tmp/iar-guard-2026-09-19
rm -rf $G && mkdir -p $G && git -C $G init -q
cp /tmp/iar-smoke-2026-09-19/brief.md $G/brief.md
echo "随便写点什么" > $G/msg.md
```

后台起一个任务（`Bash(run_in_background: true)`）：

```bash
python3 /home/xy/.claude/skills/codex-agent/.worktree/interrupt-and-resume/codex_agent.py \
  run --task iar-guard-2026-09-19 --dir /tmp/iar-guard-2026-09-19 \
  --brief /tmp/iar-guard-2026-09-19/brief.md --effort low --account default
```

等它真的跑起来（`status` 显示 running）之后，把工作目录挪走再插话：

```bash
CA=/home/xy/.claude/skills/codex-agent/.worktree/interrupt-and-resume/codex_agent.py
G=/tmp/iar-guard-2026-09-19
L=~/.codex-subagent/logs/iar-guard-2026-09-19.log
python3 $CA status iar-guard-2026-09-19        # 前提：必须是 running
mv $G $G-moved
BEFORE=$(grep -c "本轮被 INT 打断" $L)
python3 $CA interrupt-and-resume iar-guard-2026-09-19 \
  --brief $G-moved/msg.md --effort low; echo "退出码 $?"
AFTER=$(grep -c "本轮被 INT 打断" $L)
echo "打断标记 $BEFORE → $AFTER（必须相等）"
mv $G-moved $G
python3 $CA stop iar-guard-2026-09-19
```

Expected: 退出码 2，人话说「工作目录 … 不在了」；`BEFORE == AFTER` —— **闸挡在了发信号之前**。

- [ ] **Step 7: 清理并提交冒烟结论**

```bash
for t in iar-smoke-2026-09-19 iar-guard-2026-09-19; do
  rm -f ~/.codex-subagent/tasks/$t.json ~/.codex-subagent/reports/$t.md \
        ~/.codex-subagent/logs/$t.log
done
rm -rf /tmp/iar-smoke-2026-09-19 /tmp/iar-guard-2026-09-19
git commit --allow-empty -m "test: 端到端冒烟通过

打断留痕、被打断那一轮退出码 130、上下文真的接上（codex 自己说得出被打断前
建了 s1.txt）、没在跑时也能续跑、工作目录没了时闸挡在发信号之前（打断标记
计数不变）。"
```
