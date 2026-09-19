"""codex-agent 的全部单测 —— 也是这个项目的**维护者入口**。

怎么跑：

    python3 -m unittest test_codex_agent -v

（零依赖、零 codex token。纯函数和护栏全在这里，不花钱。）

这套测试被三轮突变验过，第一轮 78 个突变杀掉 57，存活的逐条补齐。
下面这些**承重约束**的突变**全部被杀**，动它们之前先想清楚你在拆什么：

    信号安全（start_new_session + 统一转发 INT + 用完还原）
    进程反查的三道过滤（uid / comm / argv 元素精确相等）
    开跑前三件事的顺序（落元数据、清旧报告、写轮次分隔符，全在 spawn 之前）
    防陈旧报告（clear_report）、日志追加而非覆盖、**轮次边界由拥有者记下而非推测**
    三种错误形式（用户层 ERROR: / tracing / 顶层 Error:）与良性 target 白名单
    退出码**五态**的绝对值、严重度排序（interrupted 在 running 之后）、元数据字段清单
    read1 的实时性、stdout flush、EPERM 即存活、strip_ansi（resume 路上承重）
    resume 的三处 flag 差异、任务名字符集、兜底句两条路都真的加上

总纲：**空测试比没测试更糟。** 它占着「这条被测过」的位置，却什么都不挡。
凡是依赖外部进程／文件的测试，先断言前提成立，前提不成立就 fail，别让它静悄悄
地绿。这条是四次踩出来的，四次还都是同一个病——断言的两边一起动：

    1. 陪练进程写成 `sleep 5 <mark>`，而 sleep 收到多余参数会立刻退出。
       进程根本不存在，于是把承重的 comm 过滤整个删掉，测试照样绿。
    2. `assertEqual(cmd_status(...), ca.EXIT["running"])`。
       把 EXIT["running"] 改成 0，两边一起动，测试照样绿——而那正是它号称
       要防的「status && deploy 在任务还在跑的时候提前部署」。
    3. `assertEqual(set(new_meta(...)), set(REQUIRED_META_KEYS))`，而后者是从
       前者派生的。构造器少一个字段，校验面跟着少，测试照样绿。
    4. `assertEqual(ca.judge(report, log_text).state, "failed")` 只钉状态，不钉
       「这个结论是从哪段日志得出的」。后来的一轮往同一个日志追加分隔符，判据
       被致盲，而这条测试照样绿——回归锁要同时钉住**结论**和**边界**
       （见 TestRoundBoundary：正面钉拥有者的偏移，反面钉「猜边界当场失明」）。

    解药一律是**再钉一条绝对值断言**：退出码钉 {success:0, failed:1,
    suspect:3, running:4, interrupted:130}，字段清单钉那六个名字，
    边界钉「这段文本从哪来」，别只钉「两边相等」。

还有一条验收判据容易被当成数字游戏：`SKILL.md` 的判据是
**「已由代码保证的约束，在文档里泄漏数 = 0」**，不是行数。
行数（现在 61 行）只说明它确实从 250 行收敛了；为了凑「≤ 50」去删内容，
删掉的只会是代码替不了的那部分（effort 分档、没有收件箱所以只能
interrupt-and-resume、退出码怎么读），正好把这次重写的目的做反。
这条判据本身也有测试守着，见 TestSkillDocDoesNotRepeatCode。
"""
import argparse
import contextlib
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

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
# 被 INT 打断几乎必然留下这一行。它的 target 是 codex_core::session，而那个
# target 刻意不在良性白名单里（非打断场景下它仍该被看见），所以它会照常进 detail。
# 注意它**不含** THREAD_LOCK_MARK／USAGE_LIMIT_MARK——那两条的处置是换账号／
# 新起任务，和「接着 resume」相反，混用会让这条测试悄悄变成另一条的副本。
ERR_ROLLOUT_ON_INTERRUPT = ("2026-09-19T12:00:00.000000Z ERROR codex_core::session: "
                            "failed to record rollout items: thread 01a0… not found")
ERR_TRACING_ROUTER = ("2026-09-18T16:49:02.380969Z ERROR codex_core::tools::router: "
                      "apply_patch failed: file changed on disk")
ERR_TRACING_WS = ("2026-09-18T16:49:02.380969Z ERROR codex_api::endpoint::responses_websocket: "
                  "websocket closed unexpectedly")
HEADER = ("Reading additional input from stdin...\n"
          "OpenAI Codex v0.154.0\n"
          "--------\n"
          "\x1b[1mworkdir:\x1b[0m /home/xy/repo\n"
          "\x1b[1msession id:\x1b[0m 01a0b408-f718-7ff3-8123-d5202551acba\n"
          "--------\n")


@contextlib.contextmanager
def _no_codex():
    """把 spawn 换成一个立刻 EOF 的假进程。

    run_codex 的真实逻辑（落元数据、清报告、写分隔符、tee、还原信号）照跑，
    但不真的把 codex 叫起来——单测绝不能真的发网络请求。
    这里刻意 patch 的是 subprocess.Popen 而不是 run_codex 本身：清报告和写分隔符
    就住在 run_codex 里，把它整个 mock 掉，要验的行为就一起没了。
    """
    with mock.patch.object(ca.subprocess, "Popen") as popen:
        popen.return_value.stdout.read1.return_value = b""
        popen.return_value.wait.return_value = 0
        yield popen


def _full_meta(task, **over):
    """元数据的完整形状。_load_meta 会校验必填键，测试不能再写半截字典。"""
    meta = {"task": task, "account": "default", "dir": "/tmp", "effort": "low",
            "session_id": None, "started_at": "2026-09-19T00:00:00"}
    meta.update(over)
    return meta


class _HomeSandbox(unittest.TestCase):
    """把 HOME 整个搬进临时目录。

    两个 patch 缺一不可：`Path.home()` 和 `Path.expanduser()` 是两条路——
    后者走 os.path.expanduser 读 $HOME，不受 Path.home 的 patch 影响，
    而 cmd_run 里就有 .expanduser()。
    """

    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(ca.pathlib.Path, "home", staticmethod(lambda: self.home)),
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
        ]
        for q in self.patches:
            q.start()
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").write_text("{}")

    def tearDown(self):
        for q in reversed(self.patches):
            q.stop()


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
        # `^ERROR:` 大小写敏感，匹配不到 `Error:`——而形式 C 正是最该报的那条
        got = ca.runtime_error_lines(ERR_FATAL)
        self.assertEqual(len(got), 1)
        self.assertIn("already has an active writer", got[0])

    def test_良性target被过滤(self):
        for benign in (ERR_TRACING, ERR_TRACING_ROUTER, ERR_TRACING_WS):
            with self.subTest(line=benign[:60]):
                self.assertEqual(ca.runtime_error_lines(benign), [])

    def test_Reconnecting按前缀过滤_后缀有多种写整行会漏(self):
        for suffix in ("2/5", "5/5", "waiting for network"):
            with self.subTest(suffix=suffix):
                self.assertEqual(ca.runtime_error_lines(f"ERROR: Reconnecting... {suffix}"), [])
        self.assertEqual(ca.runtime_error_lines(ERR_RECONNECT), [])

    def test_codex_core_session不是良性_它是最该报的那类(self):
        self.assertEqual(len(ca.runtime_error_lines(ERR_TRACING_UNKNOWN)), 1)

    def test_子进程输出和brief原文不算运行时ERROR(self):
        noise = "\n".join([
            "error[E0599]: no method named `cols_at` found for struct `Arc<Broker>`",
            "E   KeyError: ('2026-07-28', '1min/feature/ewm_std_hl120')",
            "**Test errors?** Fix error, re-run until it fails correctly.",
            "## Warning Signs",
            "error: test failed, to rerun pass `--lib`",
            "Error handling is covered in section 3.",   # 形式 C 要 `Error:` 才算
        ])
        self.assertEqual(ca.runtime_error_lines(noise), [])


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


class TestRoundBoundaryWiring(_HomeSandbox):
    """谁用哪种边界，是这次改动的全部意义所在。

    单元层的 read_round 全绿、cmd_run 却接成 read_last_round —— bug 一点没修。
    所以三条路各钉一条：run 和 resume 用 run_codex 回传的本轮文本，
    status 用最后一轮。
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

    def test_run收尾用的是run_codex回传的本轮文本(self):
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

    def test_resume收尾用的是run_codex回传的本轮文本(self):
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

    def test_还在跑时status根本不读日志_那一支用不到它(self):
        # judge 在「还在跑」这一支根本不碰 round_text，而 status 正是轮询用的
        # 热路径。实测 read_last_round：0.83MB 约 10ms、8.3MB 约 100ms，
        # 不带任务名时还要乘任务数——读出来再丢掉是白烧。
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t"))
        ca._log_path(d, "t").write_text("随便什么\n")
        args = ca.build_parser().parse_args(["status", "t"])
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "read_last_round") as r:
            self.assertEqual(ca.cmd_status(args), 4)   # 绝对值：还在跑就是 4
        r.assert_not_called()

    def test_status用的是最后一轮_外部观察者只能看最后一轮(self):
        # 反向钉一道：status 没有偏移可用，它只能看最后一轮，也**只该**看最后一轮。
        # 接成别的（比如从头扫）会把上一轮的打断标记算到这一轮头上。
        #
        # 这里 spy 的是 judge 而不是 _print_verdict：cmd_status 自己排版、
        # 根本不走 _print_verdict（patch 它只会拿到空列表，是条空测试）。
        # spy 还顺手把**传给判据的那段文本**也钉住了——这正是「谁用哪种边界」的本体。
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
        self.assertNotIn(ca.INTERRUPT_MARK, texts[0],
                         "status 把上一轮的打断标记读进了本轮")
        self.assertNotIn("resume", seen[0].reason,
                         "上一轮的打断标记被算到了这一轮头上")


class TestExtractSessionId(unittest.TestCase):
    def test_从带ANSI的日志头提取(self):
        self.assertEqual(ca.extract_session_id(HEADER),
                         "01a0b408-f718-7ff3-8123-d5202551acba")

    def test_还没打出来时返回None(self):
        self.assertIsNone(ca.extract_session_id("OpenAI Codex v0.154.0\n"))


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

        它推翻的是一个写明了理由的旧决定（`assertEqual(v.state, "failed")  # 产物
        确实没出来，状态不变`）。正面回应：产物没出来是事实，但状态回答的不是
        「产物出来没有」，而是「接下来该干什么」——五个状态都是按这个轴分的，
        而被打断的处置（接着 resume）和其余四个都不同。
        """
        v = self._judge(ca.INTERRUPT_MARK + "\n")
        self.assertEqual(v.state, "interrupted")
        self.assertIn("resume", v.reason)

    def test_撞额度上限比被打断更该被说出来(self):
        # 两个都命中时，「换账号」比「可以 resume」更接近真正的处置
        self.assertIn("额度", self._judge(ERR_USER_LAYER + "\n" + ca.INTERRUPT_MARK + "\n").reason)

    def test_非UTF8的报告不许把判据打崩(self):
        # codex 被 SIGINT 打断时可能只写出半截字节
        self.report.write_bytes(b"\xff\xfe" + "干完了".encode())
        self.assertEqual(self._judge().state, "success")


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
        v = ca.judge(self.report, ca.INTERRUPT_MARK + "\n")
        self.assertEqual(v.state, "interrupted")
        self.assertEqual(ca.EXIT[v.state], 130)
        self.assertIn("resume", v.reason)

    def test_额度上限和写锁仍然是failed_它们resume救不回来(self):
        # 这两条的补救是换账号／新起任务，不是 resume——不许被第五态顺手吃掉
        for mark in (ca.USAGE_LIMIT_MARK, ca.THREAD_LOCK_MARK):
            with self.subTest(mark=mark):
                v = ca.judge(self.report, mark + "\n" + ca.INTERRUPT_MARK + "\n")
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
        for other in (ca.USAGE_LIMIT_MARK, ca.THREAD_LOCK_MARK):
            self.assertNotIn(other, ERR_ROLLOUT_ON_INTERRUPT,
                             "前提不成立：样本行自带更坏的标记，这条测的不是共存优先级")
        v = ca.judge(self.report, ERR_ROLLOUT_ON_INTERRUPT + "\n" + ca.INTERRUPT_MARK + "\n")
        self.assertEqual(v.state, "interrupted")
        self.assertEqual(len(v.detail), 1)
        self.assertIn("codex_core::session", v.detail[0])


class TestIsolation(_HomeSandbox):
    def setUp(self):
        super().setUp()
        (self.home / ".codex-accounts" / "acct2").mkdir(parents=True)
        (self.home / ".codex-accounts" / "acct2" / "auth.json").write_text("{}")

    def test_账号可选项来自实际目录扫描(self):
        self.assertEqual(ca.account_choices(), ["default", "acct2"])

    def test_default账号映射到不带后缀的隔离目录(self):
        self.assertEqual(ca.isolation_home("default"), self.home / ".codex-subagent")
        self.assertEqual(ca.isolation_home("acct2"), self.home / ".codex-subagent-acct2")

    def test_首次使用自动建齐目录与配置(self):
        d = ca.ensure_isolation("acct2")
        for sub in ("skills", "plugins", "tasks", "reports", "logs"):
            with self.subTest(sub=sub):
                self.assertTrue((d / sub).is_dir())
        self.assertTrue((d / "config.toml").is_file())
        self.assertTrue((d / "auth.json").is_symlink())

    def test_生成的config不写模型与沙箱_那些由CLI每次显式传(self):
        # 写进 config 就是同一条事实有两个家，还是个会被静默覆盖的缺省值
        d = ca.ensure_isolation("acct2")
        text = (d / "config.toml").read_text()
        for key in ("model", "model_reasoning_effort", "sandbox_mode", "approval_policy"):
            with self.subTest(key=key):
                self.assertNotIn(f"{key} =", text)

    def test_config被codex追加过内容也不重写_那是它的trust状态(self):
        d = self.home / ".codex-subagent"
        d.mkdir()
        (d / "config.toml").write_text(ca.CONFIG_NOTE + '\n[projects."/x"]\ntrust_level = "trusted"\n')
        ca.ensure_isolation("default")
        self.assertIn("trust_level", (d / "config.toml").read_text())

    def test_共享扫描根非空就拒跑_CODEX_HOME管不到它(self):
        (self.home / ".agents" / "skills" / "某个skill").mkdir(parents=True)
        with self.assertRaises(ca.Rejected) as cm:
            ca.ensure_isolation("default")
        self.assertIn("某个skill", cm.exception.message)

    def test_auth指错账号时被改回来(self):
        d = ca.ensure_isolation("default")
        (d / "auth.json").unlink()
        (d / "auth.json").symlink_to(self.home / ".codex-accounts" / "acct2" / "auth.json")
        ca.ensure_isolation("default")
        self.assertEqual(os.readlink(d / "auth.json"), str(self.home / ".codex" / "auth.json"))

    def test_config是软链就拒跑_隔离会失效(self):
        d = self.home / ".codex-subagent"
        d.mkdir()
        (d / "config.toml").symlink_to(self.home / ".codex" / "config.toml")
        with self.assertRaises(ca.Rejected) as cm:
            ca.ensure_isolation("default")
        self.assertIn("软链", cm.exception.message)

    def test_已有的config不被覆盖(self):
        d = self.home / ".codex-subagent"
        d.mkdir()
        (d / "config.toml").write_text('model = "自定义"\n')
        ca.ensure_isolation("default")
        self.assertIn("自定义", (d / "config.toml").read_text())

    def test_账号没登录态就拒跑(self):
        (self.home / ".codex-accounts" / "acct3").mkdir()
        with self.assertRaises(ca.Rejected) as cm:
            ca.ensure_isolation("acct3")
        self.assertIn("登录", cm.exception.message)


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

    def test_主线从源头关掉颜色(self):
        # --color auto（默认）在输出被重定向时并不关颜色，106 份日志无一例外含 ANSI
        argv = ca.build_run_argv("/abs/repo", "low", "/d/reports/t.md", "b")
        self.assertEqual(argv[argv.index("--color") + 1], "never")

    def test_resume不许出现color_它也不认(self):
        # 2026-09-19 端到端冒烟实测：resume 见到 --color 直接
        # `error: unexpected argument '--color' found`，整轮当场死掉。
        # 而且 codex 没有对应的 config 键（--strict-config 探测：
        # unknown configuration field `color`），所以 resume 这条路关不掉颜色，
        # 只能靠解析侧的 strip_ansi 兜——它在 resume 这条路上是承重的。
        argv = ca.build_resume_argv("/abs/repo", "s", "low", "/d/reports/t.md", "b")
        self.assertNotIn("--color", argv)

    def test_环境变量把会话索引留在主目录_resume才找得到(self):
        env = ca.codex_env(pathlib.Path("/d"))
        self.assertEqual(env["CODEX_HOME"], "/d")
        self.assertEqual(env["CODEX_SQLITE_HOME"], str(pathlib.Path.home() / ".codex"))


class TestFixedArgs(unittest.TestCase):
    """两条命令都必须带上的固定参数。

    这些是「调用方碰不到、也就不可能漏掉」的那一批，但没人测就等于没锁：
    审查实测把 approval_policy / project_doc_max_bytes / --disable plugins /
    --skip-git-repo-check 逐个删掉，测试一个都不响。
    主线和 resume **各跑一遍**——两条命令的参数集不一样，只测一条会漏。
    """

    CASES = {
        "run": lambda: ca.build_run_argv("/abs/repo", "low", "/d/reports/t.md", "brief"),
        "resume": lambda: ca.build_resume_argv("/abs/repo", "s1", "low", "/d/reports/t.md", "brief"),
    }

    def test_批准策略永远是never_否则会停下来等人点确认(self):
        for name, build in self.CASES.items():
            with self.subTest(cmd=name):
                self.assertIn('approval_policy="never"', build())

    def test_不读仓库的AGENTS_md_那是隔离的一部分(self):
        for name, build in self.CASES.items():
            with self.subTest(cmd=name):
                self.assertIn("project_doc_max_bytes=0", build())

    def test_plugins被禁掉(self):
        for name, build in self.CASES.items():
            with self.subTest(cmd=name):
                argv = build()
                self.assertEqual(argv[argv.index("--disable") + 1], "plugins")

    def test_跳过git仓库检查_否则非仓库目录起不来(self):
        for name, build in self.CASES.items():
            with self.subTest(cmd=name):
                self.assertIn("--skip-git-repo-check", build())

    def test_模型固定且就是这一个(self):
        # 绝对值：写成 `== ca.MODEL` 的话，改掉 MODEL 两边一起动，等于没测
        self.assertEqual(ca.MODEL, "gpt-6-astra")
        for name, build in self.CASES.items():
            with self.subTest(cmd=name):
                argv = build()
                self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-astra")

    def test_难度分档如实传给codex(self):
        for name, build in self.CASES.items():
            with self.subTest(cmd=name):
                self.assertIn('model_reasoning_effort="low"', build())


class TestMeta(_HomeSandbox):
    def test_元数据写入后能跨隔离目录查回来_home走返回值不是魔法键(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t1", _full_meta("t1", dir="/abs/x"))
        home, meta = ca.find_meta("t1")
        self.assertEqual(meta["dir"], "/abs/x")
        self.assertEqual(home, d)
        # 魔法键意味着每个写回元数据的地方都得记得剥掉它
        self.assertNotIn("_home", meta)

    def test_查不到返回一对None(self):
        self.assertEqual(ca.find_meta("不存在的任务"), (None, None))

    def test_元数据缺字段就拒绝_不给默认值圆场(self):
        d = ca.ensure_isolation("default")
        ca.meta_path(d, "broken").write_text('{"task": "broken"}')
        with self.assertRaises(ca.Rejected) as cm:
            ca.find_meta("broken")
        self.assertIn("缺字段", cm.exception.message)

    def test_同名任务出现在两个隔离目录就拒绝_不许猜(self):
        # 这条路很好走：撞额度上限 → 换账号重跑同名任务
        (self.home / ".codex-accounts" / "acct2").mkdir(parents=True)
        (self.home / ".codex-accounts" / "acct2" / "auth.json").write_text("{}")
        for account in ("default", "acct2"):
            ca.write_meta(ca.ensure_isolation(account), "clash", _full_meta("clash", account=account))
        with self.assertRaises(ca.Rejected) as cm:
            ca.find_meta("clash")
        self.assertIn("多个隔离目录", cm.exception.message)

    def test_列出全部任务(self):
        d = ca.ensure_isolation("default")
        for name in ("a", "b"):
            ca.write_meta(d, name, _full_meta(name))
        self.assertEqual(sorted(m["task"] for _, m in ca.all_metas()), ["a", "b"])


class TestMetaShape(_HomeSandbox):
    """元数据的形状只许有一个家。

    突变掉 `cmd_run` 手拼字典里的一个 `effort` 之后的真实后果：run 照常报成败，
    但那个任务从此 status/resume/stop **全够不着**（`_load_meta` 一律拒绝），
    而工具给出的唯一建议「删掉它重新 run」会把会话**彻底弄丢**。
    """

    def setUp(self):
        super().setUp()
        self.workdir = self.home / "repo"
        self.workdir.mkdir()
        self.brief = self.home / "brief.md"
        self.brief.write_text("干活")

    # 字段清单的**绝对值**。REQUIRED_META_KEYS 是从 new_meta 派生的，所以
    # 「构造器的键集 == 校验面」那条断言两边会一起动——构造器少一个字段，
    # 校验面跟着少，测试照样绿（实测过）。和退出码一样，得按绝对值钉。
    FIELDS = {"task", "account", "dir", "effort", "session_id", "started_at"}

    def test_字段清单的绝对值(self):
        self.assertEqual(set(ca.REQUIRED_META_KEYS), self.FIELDS)
        # resume 要回到同一个目录、同一个会话，这两个字段是它的命根子
        self.assertIn("dir", self.FIELDS)
        self.assertIn("session_id", self.FIELDS)
        # 存活必须每次现查：存下来的 PID 会过期、会被系统复用
        self.assertNotIn("pid", self.FIELDS)

    def test_构造器的键集就是校验面(self):
        self.assertEqual(set(ca.new_meta("t", "default", "/abs/x", "low")),
                         set(ca.REQUIRED_META_KEYS))

    def test_run落盘的元数据键集与校验面相等_不多不少(self):
        # 相等而不是包含：少一个字段任务就够不着了；多塞一个 pid 又会破坏
        # 「存活必须每次现查」那条设计意图（存下来的 PID 会过期、会被复用）。
        d = ca.ensure_isolation("default")
        args = ca.build_parser().parse_args(
            ["run", "--task", "t", "--dir", str(self.workdir), "--brief", str(self.brief),
             "--effort", "low", "--account", "default"])
        with _no_codex():
            ca.cmd_run(args)
        self.assertEqual(set(json.loads(ca.meta_path(d, "t").read_text())),
                         set(ca.REQUIRED_META_KEYS))


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

    def test_只看当前用户的进程_别人的codex不进候选集(self):
        # 本机有别的用户同时在跑 codex，不限用户的话 stop 会把 SIGINT 打到别人身上。
        # 没法真拿别人的账号起进程，就反过来做：把「当前用户」换成别人，
        # 我们自己这个 comm=codex、argv 对得上的进程就该落选。
        tmp = pathlib.Path(tempfile.mkdtemp())
        fake = self._fake_codex(tmp)
        (tmp / "reports").mkdir()
        mark = str(tmp / "reports" / "t.md")
        pathlib.Path(mark).write_text("")
        proc = subprocess.Popen([str(fake), "-f", mark], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self._wait_argv(proc, mark)
            self.assertEqual(ca.find_codex_pid(mark), proc.pid)   # 前提：本来找得到
            with mock.patch.object(ca.os, "getuid", return_value=os.getuid() + 12345):
                self.assertIsNone(ca.find_codex_pid(mark))
        finally:
            proc.kill()
            proc.wait()

    @staticmethod
    def _argv_of(pid):
        return pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")

    def _wait_argv(self, proc, needle):
        """等进程真的 exec 完、argv 里出现 needle。前提不成立就 fail，不让测试空转。"""
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if needle in self._argv_of(proc.pid):
                    return
            except (FileNotFoundError, ProcessLookupError):
                pass
            time.sleep(0.05)
        self.fail(f"陪练进程的 argv 里始终没有 {needle}，本测试无法验证任何东西")

    @staticmethod
    def _fake_codex(tmp):
        """把一个真二进制命名成 codex —— comm 就会报 codex。"""
        fake = tmp / "codex"
        fake.write_bytes(pathlib.Path("/usr/bin/tail").read_bytes())
        fake.chmod(0o755)
        return fake

    def test_只认comm是codex的进程_别的进程不算(self):
        # 2026-09-19 实测：按报告路径反查会命中发命令的 bash 自己（comm=bash），
        # comm 过滤是承重的，不是保险。
        # 陪练进程把报告路径作为 argv 里**独立一项**传进去，和 codex 的 `-o <路径>`
        # 形状一致——否则测到的只是「没匹配上」，不是「comm 把它挡住了」。
        mark = "/tmp/codex-agent-selftest-不存在的报告.md"
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", mark])
        try:
            self._wait_argv(proc, mark)
            self.assertIsNone(ca.find_codex_pid(mark))
        finally:
            proc.kill()
            proc.wait()

    def test_comm真是codex的进程会被找到_反向也要成立(self):
        # 只测"排除"的话，一个永远返回 None 的实现也能全绿。
        tmp = pathlib.Path(tempfile.mkdtemp())
        fake = self._fake_codex(tmp)
        (tmp / "reports").mkdir()
        mark = str(tmp / "reports" / "t.md")
        pathlib.Path(mark).write_text("")
        proc = subprocess.Popen([str(fake), "-f", mark], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self._wait_argv(proc, mark)
            self.assertEqual(ca.find_codex_pid(mark), proc.pid)
        finally:
            proc.kill()
            proc.wait()

    def test_报告路径必须是argv里独立一项_出现在brief正文里不算(self):
        """brief 是 codex argv 里的最后一项，正文里完全可能提到别的任务的报告路径。

        按子串匹配的话那就成了误命中：这个进程干的根本不是那个任务，
        却会被 status 报成 running、被 stop 打中、让 run 以「还在跑」误拒。
        报告路径在 codex 的 argv 里正好是独立一项（`-o <路径>`），
        所以比对必须是**元素相等**，不是子串包含。
        """
        tmp = pathlib.Path(tempfile.mkdtemp())
        fake = self._fake_codex(tmp)
        (tmp / "reports").mkdir()
        mine = str(tmp / "reports" / "mine.md")
        others = str(tmp / "reports" / "others.md")
        pathlib.Path(mine).write_text("")
        brief = f"请参考 {others} 里的结论再动手"      # 别的任务的报告路径只出现在正文里
        proc = subprocess.Popen([str(fake), "-f", mine, brief], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self._wait_argv(proc, brief)
            self.assertEqual(ca.find_codex_pid(mine), proc.pid)   # 前提：自己找得到自己
            self.assertIsNone(ca.find_codex_pid(others))
        finally:
            proc.kill()
            proc.wait()

    def test_任务名里的点不是通配符_不许命中别的任务(self):
        """任务名里的 `.` 绝不能被当成通配符。

        旧实现用 `pgrep -f <报告路径>`，而那是**正则**：任务名 `a` 的
        `…/reports/a.md` 会命中任务 `aXmd` 的 `…/reports/aXmd.md`
        （`a` + 任意字符 + `md`）——2026-09-19 实测 `pgrep -f …/a.md` 确实
        返回了 aXmd 那个进程的 pid。
        后果是实打实的：`codex-agent stop a` 把 SIGINT 发给 `aXmd` 的 codex，
        而 `run --task a` 会被「还在跑」误拒。
        所以反查必须是 **argv 精确元素匹配**，不能有任何正则语义。
        """
        tmp = pathlib.Path(tempfile.mkdtemp())
        fake = self._fake_codex(tmp)
        (tmp / "reports").mkdir()
        victim = str(tmp / "reports" / "aXmd.md")   # 任务 aXmd 的报告路径
        hunter = str(tmp / "reports" / "a.md")      # 任务 a 的报告路径
        pathlib.Path(victim).write_text("")
        proc = subprocess.Popen([str(fake), "-f", victim], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self._wait_argv(proc, victim)
            # 前提：它找得到自己，否则下面那条断言是空的
            self.assertEqual(ca.find_codex_pid(victim), proc.pid)
            self.assertIsNone(ca.find_codex_pid(hunter))
        finally:
            proc.kill()
            proc.wait()


class TestSkillDocDoesNotRepeatCode(unittest.TestCase):
    """SKILL.md 的验收判据：已由代码保证的约束，一条都不许在文档里再说一遍。

    把文档约定变成硬约束，正是这个项目的主张本身——所以这条判据自己也得是
    一条测试，而不是又一句靠人记住的约定。
    每多说一遍就是第二个家：改了代码忘了改文档，文档就开始说假话，
    而读文档的人没有任何办法发现。
    """

    # 左边是模式，右边是「这条约束现在住在代码的哪儿」
    OWNED_BY_CODE = {
        r"--cd": "build_run_argv / build_resume_argv",
        r"/dev/null|DEVNULL": "run_codex 的 stdin=subprocess.DEVNULL",
        r"/proc|pgrep|pkill": "find_codex_pid",
        r"kill -INT|SIGTERM|SIGINT": "run_codex 的信号转发 + cmd_stop",
        r"mkdir -p": "ensure_isolation",
        r"--color": "build_run_argv（resume 不认它）",
        r"CODEX_HOME|CODEX_SQLITE_HOME": "codex_env",
        r"--sandbox|sandbox_mode": "build_run_argv / build_resume_argv",
        r"--disable|project_doc_max_bytes|approval_policy": "_COMMON",
        r"session id|session_id": "extract_session_id / 元数据",
        r"\bexit 1\b|退出码不可信": "judge（判据只看产物和日志）",
    }

    def test_没有一条代码级约束泄漏进文档(self):
        skill = (pathlib.Path(ca.__file__).parent / "SKILL.md").read_text()
        leaked = {pat: owner for pat, owner in self.OWNED_BY_CODE.items()
                  if re.search(pat, skill)}
        self.assertEqual(leaked, {},
                         "这些约束已经由代码保证，文档里不该再说一遍："
                         + "；".join(f"{p} → 归 {o}" for p, o in leaked.items()))

    def test_文档仍然保留代码替不了的那部分(self):
        """反向守一道：别为了让上面那条变绿，把该留的也删了。

        断言的是**具体内容**，不是「某个词出现过」——后者近乎空测试：
        把 effort 分档表整张删掉，`--effort low` 那行还在，`effort` 这个词
        照样搜得到（实测这条突变存活过）。
        """
        skill = (pathlib.Path(ca.__file__).parent / "SKILL.md").read_text()
        # 五个难度档位一个都不能少：派什么活用哪档，是判断力，代码替不了
        for tier in ca.EFFORTS:
            with self.subTest(tier=tier):
                self.assertIn(tier, skill)
        # 五条命令都得在，否则调用方不知道有这些能力
        for cmd in ("run", "status", "resume", "stop", "interrupt-and-resume"):
            with self.subTest(cmd=cmd):
                self.assertIn(f"codex-agent {cmd}", skill)
        # 三件代码保证不了、只能靠调用方知道的事
        self.assertIn("run_in_background", skill)   # 启动方式，工具自己判断不了
        self.assertIn("brief", skill)               # brief 只收文件路径
        for code in ("0", "1", "3", "4", "130"):    # 退出码是对外契约
            with self.subTest(code=code):
                self.assertIn(f"`{code}`", skill)
        # 「要不要为此打断」是判断力，代码替不了：它要知道这条信息值多少、
        # 在途工作损失多少，后者在 codex 里根本不可观测
        self.assertIn("值不值", skill)


class TestTaskName(unittest.TestCase):
    def test_收正常任务名(self):
        for good in ("smoke-2026-09-19", "a.b_c", "T1"):
            with self.subTest(good=good):
                self.assertEqual(ca.task_name(good), good)

    def test_拒会咬到文件系统的字符(self):
        # 任务名会被直接拼成三个文件名：`a/b` 写不出文件、`../x` 写到 tasks/ 外面。
        # （`a|b` 这类正则元字符曾经还会咬反查，那条已经由 /proc 精确匹配解决，
        #   但限制保留——文件名这条理由依然成立。）
        for bad in ("../escape", "a/b", "a|b", "fix(api)", "", "a b"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                ca.task_name(bad)


class TestParser(_HomeSandbox):
    def test_run的五个参数一个都不能少(self):
        parser = ca.build_parser()
        for missing in ["--task", "--dir", "--brief", "--effort", "--account"]:
            argv = ["run", "--task", "t", "--dir", "/tmp", "--brief", "b.md",
                    "--effort", "low", "--account", "default"]
            i = argv.index(missing)
            del argv[i:i + 2]
            with self.subTest(missing=missing), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_effort五个档位都收(self):
        parser = ca.build_parser()
        for e in ca.EFFORTS:
            with self.subTest(effort=e):
                args = parser.parse_args(["run", "--task", "t", "--dir", "/tmp",
                                          "--brief", "b.md", "--effort", e, "--account", "default"])
                self.assertEqual(args.effort, e)

    def test_effort只收这五个档位(self):
        parser = ca.build_parser()
        # codex 对 `-c model_reasoning_effort=bogus` 静默接受、banner 照打，
        # argparse 的 choices 是唯一守门员
        for bad in ("中等", "ultra", "minimal"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parser.parse_args(["run", "--task", "t", "--dir", "/tmp", "--brief", "b.md",
                                   "--effort", bad, "--account", "default"])

    def test_任务名校验挂在五个子命令上_结构上绕不过(self):
        parser = ca.build_parser()
        for argv in (["run", "--task", "a|b", "--dir", "/tmp", "--brief", "b.md",
                      "--effort", "low", "--account", "default"],
                     ["status", "a|b"], ["resume", "a|b", "--brief", "b.md", "--effort", "low"],
                     ["stop", "a|b"],
                     ["interrupt-and-resume", "a|b", "--brief", "b.md", "--effort", "low"]):
            with self.subTest(cmd=argv[0]), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_resume和stop不收account_账号是查出来的(self):
        parser = ca.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["resume", "t", "--brief", "b.md", "--effort", "low",
                               "--account", "default"])

    def test_不提供会造成误用的参数(self):
        """这六个参数是**刻意不提供**的，各有各的理由：

        | 参数 | 为什么不给 |
        |---|---|
        | `--timeout` | 会误杀正当的长任务。codex 动辄跑几十分钟，没有一个安全的默认值 |
        | `--background` | 后台与否由调用方的 `Bash(run_in_background)` 决定，工具自己加只会变成孤儿进程 |
        | `-o` | 报告路径由工具派生（`<隔离目录>/reports/<任务>.md`）。让调用方指定就会落进仓库、被 `git add -A` 收走 |
        | `--log` | 同上，日志路径也由任务名派生，两个任务才不会互相覆盖 |
        | `--model` | 模型固定 `gpt-6-astra`，难度只由 `--effort` 分档。换模型换不来正确性 |
        | `--sandbox` | 固定 `danger-full-access`；而且 resume 根本不认这个 flag，给了只会让人写出跑不起来的命令 |

        前两个是「会造成误用」，后四个是「由工具派生」。
        """
        parser = ca.build_parser()
        for bad in ["--timeout", "--background", "-o", "--log", "--model", "--sandbox"]:
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parser.parse_args(["run", "--task", "t", "--dir", "/tmp", "--brief", "b.md",
                                   "--effort", "low", "--account", "default", bad, "x"])


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


class TestRunGuards(_HomeSandbox):
    def setUp(self):
        super().setUp()
        self.workdir = self.home / "repo"
        self.workdir.mkdir()
        self.brief = self.home / "brief.md"
        self.brief.write_text("干活")

    def _args(self, **over):
        argv = ["run", "--task", over.get("task", "t"), "--dir", over.get("dir", str(self.workdir)),
                "--brief", over.get("brief", str(self.brief)), "--effort", "low", "--account", "default"]
        return ca.build_parser().parse_args(argv)

    def test_dir不是目录就拒跑(self):
        with self.assertRaises(ca.Rejected) as cm:
            ca.cmd_run(self._args(dir=str(self.home / "没有这个目录")))
        self.assertIn("不是目录", cm.exception.message)

    def test_护栏拒绝的退出码和codex失败不同码(self):
        with self.assertRaises(ca.Rejected) as cm:
            ca.cmd_run(self._args(dir=str(self.home / "没有这个目录")))
        self.assertEqual(cm.exception.code, ca.USAGE_ERROR)
        self.assertNotEqual(cm.exception.code, ca.EXIT["failed"])

    def test_brief不是文件就拒跑(self):
        with self.assertRaises(ca.Rejected) as cm:
            ca.cmd_run(self._args(brief=str(self.home / "没有这个文件.md")))
        self.assertIn("brief", cm.exception.message)

    def test_同名任务还在跑就拒绝(self):
        ca.write_meta(ca.ensure_isolation("default"), "t", _full_meta("t"))
        with mock.patch.object(ca, "find_codex_pid", return_value=99999):
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_run(self._args(task="t"))
        self.assertIn("还在跑", cm.exception.message)

    def test_同名任务属于别的账号就拒绝_否则留下够不着的孤儿元数据(self):
        (self.home / ".codex-accounts" / "acct2").mkdir(parents=True)
        (self.home / ".codex-accounts" / "acct2" / "auth.json").write_text("{}")
        ca.write_meta(ca.ensure_isolation("acct2"), "t", _full_meta("t", account="acct2"))
        with self.assertRaises(ca.Rejected) as cm:
            ca.cmd_run(self._args(task="t"))
        self.assertIn("acct2", cm.exception.message)

    def test_开跑前删掉上一轮的报告_否则旧报告会被判成本轮成功(self):
        d = ca.ensure_isolation("default")
        (d / "reports" / "t.md").write_text("上一轮的报告")
        with _no_codex() as popen:
            ca.cmd_run(self._args(task="t"))
        self.assertTrue(popen.called)
        self.assertFalse((d / "reports" / "t.md").exists())

    def test_dir相对路径被转成绝对_相对路径启动即崩(self):
        # --cd 给相对路径，codex 启动即崩（log 无 banner + os error 2）。
        # 转绝对这一步要是没了，工具就把这个坑原样传给了 codex。
        ca.ensure_isolation("default")
        seen = {}
        os.chdir(self.home)
        args = ca.build_parser().parse_args(
            ["run", "--task", "t", "--dir", "repo", "--brief", str(self.brief),
             "--effort", "low", "--account", "default"])
        def grab(*a, **k):
            seen["argv"] = a[0]
            return mock.DEFAULT       # 别写成 `x or mock.DEFAULT`：x 是真值时就把它返回去了

        with _no_codex() as popen:
            popen.side_effect = grab
            ca.cmd_run(args)
        cd = seen["argv"][seen["argv"].index("--cd") + 1]
        self.assertTrue(pathlib.Path(cd).is_absolute(), f"--cd 拿到的是 {cd}")
        self.assertEqual(pathlib.Path(cd), self.workdir.resolve())

    def test_run也要校验隔离不变量_resume那一半补过了这一半漏了(self):
        # 「每次 run/resume 都校验」是两条路，上一轮只补了 resume。
        # 这条没人守的话，config.toml 被软链回主配置、auth.json 指错账号，
        # 主路径上全查不出来——而主路径才是绝大多数运行走的那条。
        d = ca.ensure_isolation("default")
        (d / "config.toml").unlink()
        (d / "config.toml").symlink_to(self.home / ".codex" / "config.toml")
        with self.assertRaises(ca.Rejected) as cm:
            ca.cmd_run(self._args(task="t"))
        self.assertIn("软链", cm.exception.message)

    def test_同账号已结束的同名任务允许复用_只提示不拒绝(self):
        # 刻意不一律拒绝：工具没有清理命令，一律拒绝等于任务名一次性，
        # tasks/ 只能手工去删。已结束 + 同账号这一格是安全的——报告会被清掉、
        # 日志是追加的，历史不丢。
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t"))
        with mock.patch.object(ca, "find_codex_pid", return_value=None), _no_codex() as popen:
            ca.cmd_run(self._args(task="t"))
        self.assertTrue(popen.called, "同账号、已结束的同名任务被拒了，它应该允许复用")

    def test_开跑前三件事全在spawn之前做完(self):
        # 顺序反了每一件都会坏事：
        #   清报告在 spawn 之后 -> codex 收尾写下的报告会被紧接着的 unlink 删掉
        #   写元数据在 spawn 之后 -> 中途炸了就留下一个没人管的孤儿 codex
        #   写分隔符在 spawn 之后 -> 同上，而且判据会把上一轮的错误算到这一轮头上
        d = ca.ensure_isolation("default")
        (d / "reports" / "t.md").write_text("上一轮的报告")
        (d / "logs" / "t.log").write_text("上一轮的日志\n")
        seen = {}

        def snapshot(*a, **k):
            seen["旧报告还在"] = (d / "reports" / "t.md").exists()
            seen["元数据已落盘"] = ca.meta_path(d, "t").exists()
            seen["分隔符已写入"] = ca.ROUND_MARK in (d / "logs" / "t.log").read_text()
            return mock.DEFAULT

        with _no_codex() as popen:
            popen.side_effect = snapshot
            ca.cmd_run(self._args(task="t"))
        self.assertEqual(seen, {"旧报告还在": False, "元数据已落盘": True, "分隔符已写入": True})

    def test_日志是追加的_上一轮的内容不会被冲掉(self):
        d = ca.ensure_isolation("default")
        (d / "logs" / "t.log").write_text("上一轮的日志\n")
        with _no_codex():
            ca.cmd_run(self._args(task="t"))
        text = (d / "logs" / "t.log").read_text()
        self.assertIn("上一轮的日志", text)
        self.assertIn(ca.ROUND_MARK, text)


class TestResumeGuards(_HomeSandbox):
    def setUp(self):
        super().setUp()
        self.brief = self.home / "b.md"
        self.brief.write_text("再来")
        self.workdir = self.home / "repo"
        self.workdir.mkdir()

    def _args(self, task):
        return ca.build_parser().parse_args(
            ["resume", task, "--brief", str(self.brief), "--effort", "low"])

    def test_任务不存在就报错(self):
        # 任务名必须先过 task_name 的字符集，所以这里用合法但不存在的名字，
        # 否则测到的是 argparse 的拒绝，不是 cmd_resume 的
        with self.assertRaises(ca.Rejected) as cm:
            ca.cmd_resume(self._args("no-such-task"))
        self.assertIn("没有这个任务", cm.exception.message)

    def test_还在跑就拒绝resume_写锁冲突和SIGTERM锁死长得一样(self):
        ca.write_meta(ca.ensure_isolation("default"), "t",
                      _full_meta("t", session_id="s1", dir=str(self.workdir)))
        with mock.patch.object(ca, "find_codex_pid", return_value=99999):
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_resume(self._args("t"))
        self.assertIn("还在跑", cm.exception.message)

    def test_没有session_id就拒绝(self):
        ca.write_meta(ca.ensure_isolation("default"), "t2",
                      _full_meta("t2", dir=str(self.workdir)))
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_resume(self._args("t2"))
        self.assertIn("session id", cm.exception.message)

    def test_工作目录没了就拒绝_run校验了resume也得校验(self):
        # worktree 被删后照样拼命令，codex 会以 os error 2 当场崩——
        # 和 `--cd` 给相对路径同款症状，而 run 那条路是被人话拒绝的
        ca.write_meta(ca.ensure_isolation("default"), "t4",
                      _full_meta("t4", session_id="s1", dir=str(self.home / "已经删了的worktree")))
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_resume(self._args("t4"))
        self.assertIn("不在了", cm.exception.message)

    def test_resume也要校验隔离不变量(self):
        # 隔离不变量**每次 run 和 resume 都要校验**，两条路缺一条洞就还在。
        # resume 这条路上不查的话，config.toml 被软链回主配置、auth.json 指错
        # 账号，全都查不出来——而这种失效是静默的：跑起来一切正常，
        # 只是 codex 看得见它不该看见的东西。
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t5", _full_meta("t5", session_id="s1", dir=str(self.workdir)))
        (d / "config.toml").unlink()
        (d / "config.toml").symlink_to(self.home / ".codex" / "config.toml")
        with mock.patch.object(ca, "find_codex_pid", return_value=None), _no_codex():
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_resume(self._args("t5"))
        self.assertIn("软链", cm.exception.message)

    def test_resume会刷新started_at_元数据描述的是最后一次调用(self):
        # 完整的轮次历史在日志的分隔符里，元数据只描述最后一次
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t6", _full_meta("t6", session_id="s1", dir=str(self.workdir),
                                          started_at="2020-01-01T00:00:00"))
        with mock.patch.object(ca, "find_codex_pid", return_value=None), _no_codex():
            ca.cmd_resume(self._args("t6"))
        meta = json.loads(ca.meta_path(d, "t6").read_text())
        self.assertNotEqual(meta["started_at"], "2020-01-01T00:00:00")

    def test_resume开跑前也要删报告_秒死于写锁时才不会误判成功(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t3", _full_meta("t3", session_id="s1", dir=str(self.workdir)))
        (d / "reports" / "t3.md").write_text("上一轮的报告")
        with mock.patch.object(ca, "find_codex_pid", return_value=None), _no_codex():
            ca.cmd_resume(self._args("t3"))
        self.assertFalse((d / "reports" / "t3.md").exists())


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
        # 三条闸测试都 mock 掉 wait_for_exit。不 mock 的话闸序一坏就掉进真的
        # 60 秒等待：实测「把 check_can_resume 挪到发信号之后」这个突变要
        # **180.3 秒**才红，而且报的是「收到 INT 后 60 秒还没退出」——闸序坏了，
        # 报的却是等超时，指错方向。mock 之后同一突变 2.1 秒变红，报错是
        # `Expected 'interrupt_codex' to not have been called`，正中要害。
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


class TestSkillGuardIsAlwaysPrepended(_HomeSandbox):
    """兜底句是 SKILL.md 印给调用方的**对外承诺**，两条路都必须真的加上。

    `prepend_skill_guard` 自己有纯函数单测，但那只证明「这个函数会加」，
    不证明「run 和 resume 真的调了它」。把两处都换成裸 `read_text()` 的突变
    曾经**全部存活**——一条印出去的承诺，没有任何东西守着。

    结构性防线是 CODEX_HOME 隔离（codex 结构上看不见用户的 skill），
    这句是内容层的第二道：万一哪天隔离被绕开，brief 里这句还在。
    """

    def setUp(self):
        super().setUp()
        self.workdir = self.home / "repo"
        self.workdir.mkdir()
        self.brief = self.home / "brief.md"
        self.brief.write_text("干活")

    @staticmethod
    def _brief_codex_actually_got(run_it):
        """codex argv 的最后一项就是 brief 正文。"""
        seen = {}

        def grab(*a, **k):
            seen["argv"] = a[0]
            return mock.DEFAULT

        with _no_codex() as popen:
            popen.side_effect = grab
            run_it()
        return seen["argv"][-1]

    def test_run这条路(self):
        ca.ensure_isolation("default")
        args = ca.build_parser().parse_args(
            ["run", "--task", "t", "--dir", str(self.workdir), "--brief", str(self.brief),
             "--effort", "low", "--account", "default"])
        brief = self._brief_codex_actually_got(lambda: ca.cmd_run(args))
        self.assertTrue(brief.startswith(ca.SKILL_GUARD), f"codex 实际收到的是：{brief[:60]!r}")
        self.assertIn("干活", brief)

    def test_resume这条路(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t", session_id="s1", dir=str(self.workdir)))
        args = ca.build_parser().parse_args(
            ["resume", "t", "--brief", str(self.brief), "--effort", "low"])
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            brief = self._brief_codex_actually_got(lambda: ca.cmd_resume(args))
        self.assertTrue(brief.startswith(ca.SKILL_GUARD), f"codex 实际收到的是：{brief[:60]!r}")
        self.assertIn("干活", brief)


class TestExitCodeContract(_HomeSandbox):
    """退出码是这个工具相对旧 skill 的最大增量，也是 SKILL.md 印出去的对外契约。

    这里断言的全是**绝对值**。写成 `assertEqual(cmd(...), ca.EXIT["running"])`
    是空测试：常量一改两边一起动，`EXIT["running"] = 0` 这种突变照样绿——
    而那正是「还在跑」被当成功、`status && deploy` 提前部署的那个 bug。
    """

    def setUp(self):
        super().setUp()
        self.workdir = self.home / "repo"
        self.workdir.mkdir()
        self.brief = self.home / "brief.md"
        self.brief.write_text("干活")

    def _run_args(self):
        return ca.build_parser().parse_args(
            ["run", "--task", "t", "--dir", str(self.workdir), "--brief", str(self.brief),
             "--effort", "low", "--account", "default"])

    def _resume_args(self):
        return ca.build_parser().parse_args(
            ["resume", "t", "--brief", str(self.brief), "--effort", "low"])

    def _spawner(self, d, report_text, log_extra):
        """假装 codex 跑了一轮：spawn 的那一刻决定它留下什么产物。"""
        def spawn(*a, **k):
            if report_text is not None:
                (d / "reports" / "t.md").write_text(report_text)
            if log_extra:
                with open(d / "logs" / "t.log", "a") as f:
                    f.write(log_extra + "\n")
            return mock.DEFAULT
        return spawn

    def _run(self, report_text, log_extra=""):
        d = ca.ensure_isolation("default")
        with _no_codex() as popen:
            popen.side_effect = self._spawner(d, report_text, log_extra)
            return ca.cmd_run(self._run_args())

    def _resume(self, report_text, log_extra=""):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t", session_id="s1", dir=str(self.workdir)))
        with _no_codex() as popen, mock.patch.object(ca, "find_codex_pid", return_value=None):
            popen.side_effect = self._spawner(d, report_text, log_extra)
            return ca.cmd_resume(self._resume_args())

    UNCLASSIFIED = ("2026-09-17T14:35:32.578919Z ERROR codex_core::session: "
                    "Failed to create session: thread-store conflict")

    def test_退出码的绝对值是对外契约(self):
        self.assertEqual(ca.EXIT, {"success": 0, "failed": 1, "suspect": 3, "running": 4,
                                   "interrupted": 130})
        self.assertEqual(ca.USAGE_ERROR, 2)
        # 护栏拒绝必须和五个判据结论都区分得开
        self.assertNotIn(ca.USAGE_ERROR, ca.EXIT.values())

    def test_run_正常收尾退出0(self):
        self.assertEqual(self._run("干完了"), 0)

    def test_run_没留下报告退出1(self):
        self.assertEqual(self._run(None), 1)

    def test_run_报告在但有未分类错误退出3(self):
        self.assertEqual(self._run("干完了", self.UNCLASSIFIED), 3)

    def test_resume_正常收尾退出0(self):
        self.assertEqual(self._resume("干完了"), 0)

    def test_resume_没留下报告退出1(self):
        self.assertEqual(self._resume(None), 1)

    def test_resume_报告在但有未分类错误退出3(self):
        self.assertEqual(self._resume("干完了", self.UNCLASSIFIED), 3)

    def _interrupt_and_resume(self, report_text, log_extra=""):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t", session_id="s1", dir=str(self.workdir)))
        args = ca.build_parser().parse_args(
            ["interrupt-and-resume", "t", "--brief", str(self.brief), "--effort", "low"])
        # 没在跑：这条路不发信号，直接续跑——验的是「续跑那一轮的判据结论就是退出码」
        with _no_codex() as popen, mock.patch.object(ca, "find_codex_pid", return_value=None):
            popen.side_effect = self._spawner(d, report_text, log_extra)
            return ca.cmd_interrupt_and_resume(args)

    # 新命令的四态绝对值。run 和 resume 在这里都有 0/1/3，新命令此前一条都没有——
    # TestInterruptAndResumeOrder 全把 _resume_round 或 judge mock 掉了，
    # 接线错了它们一个都测不出来。这四条走**真实** _resume_round。
    def test_interrupt_and_resume_正常收尾退出0(self):
        self.assertEqual(self._interrupt_and_resume("干完了"), 0)

    def test_interrupt_and_resume_没留下报告退出1(self):
        self.assertEqual(self._interrupt_and_resume(None), 1)

    def test_interrupt_and_resume_报告在但有未分类错误退出3(self):
        self.assertEqual(self._interrupt_and_resume("干完了", self.UNCLASSIFIED), 3)

    def test_interrupt_and_resume_续跑那轮又被打断退出130(self):
        self.assertEqual(self._interrupt_and_resume(None, ca.INTERRUPT_MARK), 130)

    def test_run_被打断退出130(self):
        # 整个改动 A 的验收点：harness 的完成通知只搬退出码，于是这个数字是
        # 「接着 resume，别重跑」唯一到得了调用方的形式。
        # 2026-09-19 真机复现过这条通知：`[exited with code N]`。
        self.assertEqual(self._run(None, ca.INTERRUPT_MARK), 130)

    def test_status_还在跑退出4_否则status_and_deploy会提前部署(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t"))
        args = ca.build_parser().parse_args(["status", "t"])
        with mock.patch.object(ca, "find_codex_pid", return_value=99999):
            self.assertEqual(ca.cmd_status(args), 4)

    def test_status_一个任务都没有时退出0_查询没查到不是失败(self):
        args = ca.build_parser().parse_args(["status"])
        self.assertEqual(ca.cmd_status(args), 0)

    def test_多任务取最该拦住调用方的那个(self):
        # 四个状态的相对顺序全部钉死：少钉一对，_SEVERITY 就能被悄悄重排。
        # failed 排在最后＝最该拦住调用方：它需要人现在就看，而 running 只需要等。
        self.assertEqual(ca._worse("suspect", "failed"), "failed")
        self.assertEqual(ca._worse("failed", "suspect"), "failed")
        self.assertEqual(ca._worse("running", "failed"), "failed")
        self.assertEqual(ca._worse("suspect", "running"), "suspect")
        self.assertEqual(ca._worse("success", "running"), "running")
        self.assertEqual(ca._worse("success", "success"), "success")


class TestRunCodexOwnsReportPath(unittest.TestCase):
    """`run_codex` 自己拥有报告路径，不接受调用方算好的 argv。

    原签名是 `run_codex(argv, env, home, task, kind, meta)`：argv 由调用方拼，
    而 run_codex 又自己重新推导报告路径去删。三条「必须记得对齐」没有任何东西
    保证——argv 里的 `-o`、被删的那个文件、元数据的文件名。三条一起违反时它
    一声不吭：clear_report 删了别的文件（防陈旧报告这条 P0 静默失效）、
    日志分隔符说谎、元数据内容和文件名对不上。
    改成回传报告路径之后，这三条在结构上就违反不了了。
    """

    def _dir(self):
        d = pathlib.Path(tempfile.mkdtemp())
        for sub in ("tasks", "reports", "logs"):
            (d / sub).mkdir()
        return d

    def test_拼命令用的报告路径就是它要删的那一个(self):
        d = self._dir()
        seen = {}

        def make_argv(report_path):
            seen["给调用方的"] = report_path
            return ["codex"]

        with _no_codex():
            ca.run_codex("run", d, "t", _full_meta("t"), make_argv)
        self.assertEqual(seen["给调用方的"], str(ca._report_path(d, "t")))

    def test_跑完之后信号处置被还原(self):
        """改全局信号处置而不还原，等于把本函数的副作用留给整个进程的余生。

        审查认为「CLI 里跑完就退出，不可达，按奥卡姆可议」——那只看了 CLI 那条路。
        单测是在进程内**直接调** run_codex 的第二个调用方：不还原的话，测试进程
        余生都带着指向一个已死 Popen 的 handler，再也响应不了 Ctrl-C。
        """
        d = self._dir()
        watched = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
        before = {s: signal.getsignal(s) for s in watched}
        with _no_codex():
            ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertEqual({s: signal.getsignal(s) for s in watched}, before)

    def test_环境变量由home派生_调用方传不进一个对不上的(self):
        d = self._dir()
        with _no_codex() as popen:
            ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertEqual(popen.call_args.kwargs["env"]["CODEX_HOME"], str(d))


class TestSignalSafety(unittest.TestCase):
    """codex 只能死于 INT——这是整个工具最不能出错的一条保证。"""

    def _run_once(self, popen):
        d = pathlib.Path(tempfile.mkdtemp())
        for sub in ("tasks", "reports", "logs"):
            (d / sub).mkdir()
        popen.return_value.stdout.read1.return_value = b""
        popen.return_value.wait.return_value = 0
        ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])

    def test_codex起在独立会话里_组信号打不到它(self):
        with mock.patch.object(ca.subprocess, "Popen") as popen:
            self._run_once(popen)
        self.assertIs(popen.call_args.kwargs["start_new_session"], True)

    def test_stdin接DEVNULL_否则codex等stdin永久挂死(self):
        # 日志只剩 "Reading additional input from stdin" + 进程 0% CPU，
        # 而那种 log 和「还在思考」长得一模一样，判不出来。
        with mock.patch.object(ca.subprocess, "Popen") as popen:
            self._run_once(popen)
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_包装器收到SIGTERM时向codex转发的是SIGINT(self):
        # 转发走 interrupt_codex（内部是 os.kill），发信号和留痕焊在一起，
        # 转发这条路不可能只做一半。
        with mock.patch.object(ca.subprocess, "Popen") as popen, \
             mock.patch.object(ca.signal, "signal") as sigsig, \
             mock.patch.object(ca.os, "kill") as kill:
            self._run_once(popen)
            handled = {c.args[0] for c in sigsig.call_args_list}
            self.assertEqual(handled, {signal.SIGTERM, signal.SIGINT, signal.SIGHUP})
            sigsig.call_args_list[0].args[1](signal.SIGTERM, None)
        kill.assert_called_once_with(popen.return_value.pid, signal.SIGINT)


class TestRunCodexStreaming(unittest.TestCase):
    def test_banner一出来就把session_id落盘_不等攒满缓冲区(self):
        """假 codex 只打一行 banner（约 50 字节）然后睡 30 秒，不到 EOF。

        用 `read(1024)` 的话父进程会一直阻塞到凑满 1024 字节或 EOF，这一行永远
        到不了元数据——实测子进程 t=0 就 flush 了 172 字节，父进程 4.06 秒（EOF
        时）才看到。后果连锁：屏幕不刷新 → 日志全程 0 字节（status 的日志判据在
        运行期完全失效）→ session_id 从来没写进元数据 → 包装进程一被杀，
        cmd_resume 只能报「没记到 session id」，上下文全丢。
        `run_codex` 在这里必须跑在**子进程的主线程**里：它要装信号 handler，
        而 signal.signal 在非主线程会直接 ValueError。
        """
        d = pathlib.Path(tempfile.mkdtemp())
        for sub in ("tasks", "reports", "logs"):
            (d / sub).mkdir()
        fake_codex = ("import sys,time;"
                      "sys.stdout.write('session id: 01a0b408-f718-7ff3-8123-d5202551acba\\n');"
                      "sys.stdout.flush();time.sleep(30)")
        driver = (
            "import json,os,pathlib,sys;"
            f"sys.path.insert(0, {str(pathlib.Path(ca.__file__).parent)!r});"
            "import codex_agent as ca;"
            f"d = pathlib.Path({str(d)!r});"
            f"ca.run_codex('run', d, 't', {_full_meta('t')!r},"
            f" lambda r: [sys.executable, '-c', {fake_codex!r}])"
        )
        # 屏幕这一路也接到文件上一起验。接的是文件不是 tty，Python 默认块缓冲，
        # 所以「这行现在就能读到」只可能来自 sys.stdout.buffer.flush()——
        # 而 SKILL.md 承诺的「面板可监控」全靠它。
        screen = d / "screen.out"
        with open(screen, "wb") as out:
            proc = subprocess.Popen([sys.executable, "-c", driver],
                                    stdout=out, stderr=subprocess.PIPE)
        try:
            deadline = time.time() + 8
            got = None
            while time.time() < deadline:
                meta_file = d / "tasks" / "t.json"
                if meta_file.exists():
                    got = json.loads(meta_file.read_text())["session_id"]
                    if got:
                        break
                if proc.poll() is not None:
                    self.fail(f"驱动进程提前退出：{proc.stderr.read().decode()}")
                time.sleep(0.05)
            self.assertEqual(got, "01a0b408-f718-7ff3-8123-d5202551acba")
            # 假 codex 还睡着、远没到 EOF，此刻日志和屏幕都必须已经有内容
            self.assertIsNone(proc.poll(), "驱动进程已经退出了，那这条测的就不是实时性")
            self.assertIn("session id", (d / "logs" / "t.log").read_text())
            self.assertIn("session id", screen.read_text())
        finally:
            proc.kill()
            proc.wait()
            proc.stderr.close()


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
        # 一场竞态而不是本函数。同一套「等它真的就位」在
        # TestSignalSafetyRealProcesses 里也是承重的。
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


class TestWrapperSpeaksImmediately(unittest.TestCase):
    """包装器自己说的话必须**当场**出现在屏幕上，不能攒到进程退出才吐。

    这条 fd 有两个写者：包装器走文本层的 print，run_codex 的 tee 走
    sys.stdout.buffer（它自己 flush）。stdout 接管道／文件时文本层是**块缓冲**的，
    于是包装器的话会一直躺在缓冲区里，到退出才随 atexit 一起吐出来——排在
    codex 整轮输出**之后**。

    2026-09-19 端到端实测拿到过这个错序：`已发 SIGINT 并在日志留痕` 和
    `已确认退出` 两句都排在 codex 整轮输出的最后面，而它们要说的恰恰是
    「此刻正在发生什么」。wait_for_exit 卡住的那 60 秒里，屏幕上更是一个字都没有。
    """

    def test_说完就能被读到_不等进程退出(self):
        d = pathlib.Path(tempfile.mkdtemp())
        screen = d / "screen.out"
        driver = (
            "import sys,time\n"
            f"sys.path.insert(0, {str(pathlib.Path(ca.__file__).parent)!r})\n"
            "import codex_agent as ca\n"
            "ca._say('这句必须当场看得到')\n"
            "time.sleep(30)\n"
        )
        # 接的是文件不是 tty，Python 默认块缓冲——所以「这行现在就读得到」
        # 只可能来自 _say 自己的 flush。
        with open(screen, "wb") as out:
            proc = subprocess.Popen([sys.executable, "-c", driver],
                                    stdout=out, stderr=subprocess.PIPE)
        try:
            deadline = time.time() + 8
            while time.time() < deadline and "这句必须当场看得到" not in screen.read_text():
                if proc.poll() is not None:
                    self.fail(f"驱动进程提前退出：{proc.stderr.read().decode()}")
                time.sleep(0.05)
            self.assertIsNone(proc.poll(), "驱动进程已经退出了，那这条测的就不是实时性")
            self.assertIn("这句必须当场看得到", screen.read_text())
        finally:
            proc.kill()
            proc.wait()
            proc.stderr.close()


class TestStop(_HomeSandbox):
    def test_只发SIGINT_绝不发SIGTERM(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t"))
        args = ca.build_parser().parse_args(["stop", "t"])
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca.os, "kill") as k:
            ca.cmd_stop(args)
        k.assert_called_once_with(4242, signal.SIGINT)


class TestSignalSafetyRealProcesses(unittest.TestCase):
    """用真进程、真信号验证整个工具最不能出错的那条保证：

    **无论谁用什么信号停包装器，codex 收到的永远只有 INT。**
    这条保证由两件事合起来兑现，缺一个洞就还在：
      ① `start_new_session=True` 把 codex 挡在组信号之外
      ② 包装器给 TERM／INT／HUP 都装 handler，统一转发 INT
    它之所以是死线：SIGTERM 之后 codex 的 thread 会被**永久锁死**，
    之后 resume 永远报 thread-store conflict，等多久都不释放，上下文全丢。
    上面 TestSignalSafety 用 mock 验的是「参数传对了没」，这里验的是
    「传对了之后，内核那一层真的照做了没」——这两件事不是一回事。

    假 codex 必须**把收到的每一个信号都记下来**，而不是收到第一个就退出：
    CPython 派发待处理信号是按信号编号**从小到大**扫的，SIGINT(2) 永远排在
    SIGTERM(15) 前面。所以「收到第一个就退出」的写法在 codex 同时挨了 TERM 和
    INT 时照样只记到 INT——测试会在 start_new_session 被删掉时照样绿。
    实测过：那样写的版本，去掉 start_new_session 和改成转发 TERM 两个突变都杀不掉。
    """

    FAKE_CODEX = (
        "import signal,sys,time\n"
        "mark = sys.argv[1]\n"
        "got = []\n"
        "def record(signum, frame):\n"
        "    got.append(signal.Signals(signum).name)\n"
        "for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):\n"
        "    signal.signal(s, record)\n"
        "sys.stdout.write('session id: 01a0b408-f718-7ff3-8123-d5202551acba\\n')\n"
        "sys.stdout.flush()\n"
        "deadline = time.time() + 30\n"
        "while time.time() < deadline:\n"
        "    time.sleep(0.05)\n"
        "    if got:\n"
        "        time.sleep(0.5)\n"           # 等一等，把同一发组信号里的其它信号收全
        "        open(mark, 'w').write(','.join(sorted(set(got))))\n"
        "        sys.exit(0)\n"
        "open(mark, 'w').write('(超时：什么信号都没收到)')\n"
    )

    def test_向包装器的进程组发TERM_codex只会收到INT(self):
        d = pathlib.Path(tempfile.mkdtemp())
        for sub in ("tasks", "reports", "logs"):
            (d / sub).mkdir()
        mark = d / "codex收到的信号.txt"
        driver = (
            "import os,pathlib,sys\n"
            f"sys.path.insert(0, {str(pathlib.Path(ca.__file__).parent)!r})\n"
            "import codex_agent as ca\n"
            f"ca.run_codex('run', pathlib.Path({str(d)!r}), 't', {_full_meta('t')!r},"
            f" lambda r: [sys.executable, '-c', {self.FAKE_CODEX!r}, {str(mark)!r}])\n"
        )
        # 包装器自己起在独立会话里，这样 killpg 只打到它那一组，不会波及测试进程
        proc = subprocess.Popen([sys.executable, "-c", driver],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                start_new_session=True)
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                f = d / "tasks" / "t.json"
                if f.exists() and json.loads(f.read_text())["session_id"]:
                    break
                if proc.poll() is not None:
                    self.fail(f"包装器提前退出：{proc.stderr.read().decode()}")
                time.sleep(0.05)
            else:
                self.fail("假 codex 没起来，本测试无法验证信号，不能算通过")

            # harness 停掉后台 Bash 任务就是这么干的：向整个进程组发 TERM
            os.killpg(proc.pid, signal.SIGTERM)

            deadline = time.time() + 10
            while time.time() < deadline and not mark.exists():
                time.sleep(0.05)
            self.assertTrue(mark.exists(), "假 codex 什么信号都没收到——转发没装上")
            self.assertEqual(mark.read_text(), "SIGINT",
                             "codex 收到了 INT 以外的信号——会话会被永久锁死")
            # 转发的同时要在日志里留下痕迹，否则下一个读判据的人只会看到
            # 「报告缺失＝没正常收尾」，而正确处置其实是 resume
            deadline = time.time() + 5
            log = d / "logs" / "t.log"
            while time.time() < deadline and ca.INTERRUPT_MARK not in log.read_text():
                time.sleep(0.05)
            self.assertIn(ca.INTERRUPT_MARK, log.read_text())
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            proc.stderr.close()
