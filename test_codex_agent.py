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
    **一轮开跑前先确认上一轮安静了**——写日志的（writer，自报身份）和占会话的
        （codex，现场反查）都停了才走；只看任一边都实测踩过（早放行／孤儿 codex）
    三种错误形式（用户层 ERROR: / tracing / 顶层 Error:）与良性 target 白名单
    退出码**五态**的绝对值、严重度排序（interrupted 在 running 之后）、元数据字段清单
    read1 的实时性、stdout flush、EPERM 即存活、strip_ansi（resume 路上承重）
    resume 的三处 flag 差异、任务名字符集、兜底句**三条路**都真的加上且**每轮派生**
    控制字符在入口就挡住（--dir / --skill，文件系统那一层根本不管）
    status 数据行前四列无空白、明细行有缩进——「机器切得开」是断言出来的，不是碰巧
    兜底句里那句「动手前先逐个读一遍」——**许可不等于指令**，没有指令就没有那行
    `cat`，judge 看不见「它压根没读」，而「不软链」的论证正架在那行 `cat` 上
    `--skill` 收下什么就原样还什么（不 resolve）——命令行、brief、日志三处必须同一个串
    白名单的形状（恒为 tuple[str]）在 parser 之下也有闸——裸 str 会被逐字符拆开
    元数据**原子替换**——status 在另一个进程里并发读，永远看不到半截 json

总纲：**空测试比没测试更糟。** 它占着「这条被测过」的位置，却什么都不挡。
凡是依赖外部进程／文件的测试，先断言前提成立，前提不成立就 fail，别让它静悄悄
地绿。这条是五次踩出来的，五次还都是同一个病——断言的两边一起动：

    1. 陪练进程写成 `sleep 5 <mark>`，而 sleep 收到多余参数会立刻退出。
       进程根本不存在，于是把承重的 comm 过滤整个删掉，测试照样绿。
    2. `assertEqual(cmd_status(...), ca.EXIT["running"])`。
       把 EXIT["running"] 改成 0，两边一起动，测试照样绿——而那正是它号称
       要防的「status && deploy 在任务还在跑的时候提前部署」。
    3. `assertEqual(set(new_meta(...)), set(REQUIRED_META_KEYS))`，而后者是从
       前者派生的。构造器少一个字段，校验面跟着少，测试照样绿。
    4. `assertEqual(ca.judge(Round(report, log_text)).state, "failed")` 只钉状态，
       不钉「这个结论是从哪段日志得出的」。后来的一轮往同一个日志追加分隔符，
       判据被致盲，而这条测试照样绿——回归锁要同时钉住**结论**和**边界**
       （见 TestRoundBoundary：正面钉拥有者的本轮文本，反面钉「猜边界当场失明」）。
    5. `test_文档仍然保留代码替不了的那部分` 只断言「某个词出现过」。把 effort
       分档表整张删掉，`--effort low` 那行还在，`effort` 这个词照样搜得到——
       实测这条突变**存活**过。反向判据也要钉具体内容。
       2026-09-20 加 `--skill`/`--no-skill` 那条时**原样又踩一次**：写成
       `assertIn("--skill", skill)`，而把表格里那一行的两个参数整个删掉之后，
       启动示例和「外加 --skill/--no-skill 二选一」那句里它们还在，突变存活。
       改成钉**配对**（`--skill` 与「绝对路径」同行、`--no-skill` 与「二选一」
       同行）并让每条事实在文档里只有一个家，两个突变才各自变红。

    解药一律是**再钉一条绝对值断言**：退出码钉 {success:0, failed:1,
    suspect:3, running:4, interrupted:130}，字段清单钉那七个名字，
    边界钉「这段文本从哪来」，别只钉「两边相等」。

**没有任何一条测试允许把真的 codex 叫起来**，这条不再靠「记得包 _no_codex」：
见本文件的 `setUpModule`，漏包当场喊出来（2026-09-20 真漏过一次）。

写突变脚本时注意 `.pyc` 缓存：CPython 按 (mtime 秒, size) 判新旧，两条**字节数
相同又在同一秒内**跑的突变会命中上一条的字节码，失败被错误归因。
一律 `rm -rf __pycache__` + `PYTHONDONTWRITEBYTECODE=1`。

还有一条验收判据容易被当成数字游戏：`SKILL.md` 的判据是
**「已由代码保证的约束，在文档里泄漏数 = 0」**，不是行数。
行数（现在 65 行，实跑 wc -l）只说明它确实从 250 行收敛了；为了凑「≤ 50」去删内容，
删掉的只会是代码替不了的那部分（effort 分档、没有收件箱所以只能
interrupt-and-resume、退出码怎么读），正好把这次重写的目的做反。
这条判据本身也有测试守着，见 TestSkillDocDoesNotRepeatCode。
"""
import argparse
import contextlib
import io
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
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
ROUND_SEP_SAMPLE = "===== codex-agent run t 2026-09-19T10:00:00 ====="
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


# 一副**必定停了**的 writer 身份：`pid_max` 这个值内核从不分配（分配区间是
# [1, pid_max)），2026-09-20 实测 /proc/sys/kernel/pid_max = 4194304。
# 前提在 setUpModule 里断言一次——默认元数据要是悄悄变成「还在写」，一大批
# 「上一轮早停了」的测试会静默测到另一条分支上去。
GONE_PID = str(2 ** 22)
GONE_START = "1"


# 「单测绝不真的把 codex 叫起来」本来是**软约定**——靠每个作者记得包一层
# _no_codex。2026-09-20 实测漏过一次：一条 cmd_run 的拒绝测试没包，而那道闸
# 当时还没装上，cmd_run 一路走到 spawn，真的起了 codex（沙箱 HOME 没登录态，
# 秒退，没花钱——但那是运气，不是设计）。
# 在这里插一次桩，之后漏包**当场喊出来**，而且不必每个作者记得任何事。
# 只挡 argv[0] 恰好是 "codex" 的那一种：陪练进程（sleep、python 驱动、
# 命名成 codex 的假二进制都走绝对路径）一个都不受影响。
# 真机冒烟（要真起 codex 的那一类）不在本文件里；将来若要加，给它一条显式豁免
# 并在那里写清为什么。
_REAL_POPEN_INIT = subprocess.Popen.__init__


def setUpModule():
    """全套测试里没有任何一条允许把真的 codex 叫起来。"""
    assert not pathlib.Path(f"/proc/{GONE_PID}").exists(), (
        f"前提不成立：/proc/{GONE_PID} 居然存在。换一个必定不存在的 pid，"
        f"否则一批「上一轮早停了」的测试会静默测到另一条分支上。")

    def no_real_codex(self, args, *a, **kw):
        argv0 = args[0] if isinstance(args, (list, tuple)) else args
        assert argv0 != "codex", (
            f"这条测试把真的 codex 叫起来了：{args!r}。少包了一层 _no_codex。")
        return _REAL_POPEN_INIT(self, args, *a, **kw)
    # **它刻意不还原**（没有 tearDownModule，也没有 addModuleCleanup）：
    # 泄漏半径是**整个测试进程的余生**，同进程里后来 import 的任何模块调
    # `Popen(["codex", ...])` 都会被它挡住。现在不可达——本仓只有这一个测试
    # 模块，只 import 一次。
    # **安全性唯一的地基是上面那行 `_REAL_POPEN_INIT` 在模块顶层、import 那一刻
    # 抓的**，所以插桩永远只有一层。实测 50 次 importlib.reload + setUpModule
    # 会叠成 RecursionError——真要多次 reload 本模块，先把这条前提想清楚。
    subprocess.Popen.__init__ = no_real_codex


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
    """元数据的完整形状。_load_meta 会校验必填键，测试不能再写半截字典。

    writer 身份默认给一副**必定停了**的（见 GONE_PID）：绝大多数测试要的前提就是
    「上一轮的 writer 早就不在了」。要「还在写」的那几条自己传 _live_writer(self)。
    """
    meta = {"task": task, "account": "default", "dir": "/tmp", "effort": "low",
            "skills": [], "session_id": None, "started_at": "2026-09-19T00:00:00",
            "writer_pid": GONE_PID, "writer_start": GONE_START}
    meta.update(over)
    return meta


# ── 陪练进程的四个助手。**必须是模块级的，不许挂在某个 TestCase 上。**
# 三个测试类都要用它们，而跨类写成 `TestPid._wait_argv(self, …)` 会在 `self`
# 上找不到兄弟方法当场炸——炸点在陪练的 `kill()` **之前**，于是 `finally` 里的
# `wait()` 会永久挂住一个 `tail -f`，**整套测试跟着挂死**（原型上挂过两次）。
def _fake_codex(tmp):
    """把一个真二进制命名成 codex —— comm 就会报 codex。"""
    fake = tmp / "codex"
    fake.write_bytes(pathlib.Path("/usr/bin/tail").read_bytes())
    fake.chmod(0o755)
    return fake


def _argv_of(pid):
    return pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")


def _wait_argv(test, proc, needle):
    """等进程真的 exec 完、argv 里出现 needle。前提不成立就 fail，不让测试空转。"""
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            if needle in _argv_of(proc.pid):
                return
        except (FileNotFoundError, ProcessLookupError):
            pass
        time.sleep(0.05)
    test.fail(f"陪练进程的 argv 里始终没有 {needle}，本测试无法验证任何东西")


def _wait_zombie(test, pid):
    """等陪练真的变成僵尸，返回它的启动时刻。

    **这条路上不许再建第二个 `subprocess.Popen`**：`Popen.__init__` 会调
    `subprocess._cleanup()`，顺手把这个僵尸回收掉，测试于是测到另一条分支上去。
    """
    deadline = time.time() + 5
    while time.time() < deadline:
        fields = ca._read_stat_fields(str(pid))
        if fields is not None and fields[0] == "Z":
            return fields[1]
        time.sleep(0.01)
    test.fail(f"陪练 {pid} 始终没变成僵尸，本测试验证不了任何东西")


def _live_writer(test):
    """起一个**真的还活着的别的进程**，返回可直接落盘的那一对 writer 身份。

    要「上一轮还在写」的测试一律用它，**不许拿本进程的身份充数**：
    `_previous_writer_alive` 对本进程恒答「停了」（见那一行注释），
    拿自己当陪练的测试会测到另一条分支上去。
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    test.addCleanup(proc.wait)
    test.addCleanup(proc.kill)          # LIFO：先 kill 再 wait，绝不挂死
    deadline = time.time() + 5
    while time.time() < deadline:
        fields = ca._read_stat_fields(str(proc.pid))
        if fields is not None and fields[0] != "Z":
            return {"writer_pid": str(proc.pid), "writer_start": fields[1]}
        time.sleep(0.01)
    test.fail("陪练没起来，本测试验证不了任何东西")


class _HomeSandbox(unittest.TestCase):
    """把 HOME 整个搬进临时目录。

    两个 patch 缺一不可：`Path.home()` 和 `Path.expanduser()` 是两条路——
    后者走 os.path.expanduser 读 $HOME，不受 Path.home 的 patch 影响，
    而 cmd_run 里就有 .expanduser()。

    **凡需要 HOME 沙箱的测试类一律继承这个类，不许手写第二份。** 手写的那份
    必然漏掉其中一条（漏的总是 `$HOME` 那条，`Path.home()` 更显眼），
    而漏掉之后测试**不会红**——它只是悄悄读写**真实** HOME 下的
    ~/.codex-subagent，把开发机上真实的任务元数据当成被测数据。
    纯函数测试不要它。
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


class TestModuleCompilesClean(unittest.TestCase):
    """源码编译不许产生任何警告。

    包装器的 stdout 是判据结论的通道（退出码之外唯一带细节的那条），
    任何警告都会混进去。2026-09-19 端到端实测撞到过：`interrupt_codex` 的
    docstring 里写了正则 `(\\s\\[.*\\])?$` 却没转义，于是**每次调用**都先打一行
    `SyntaxWarning: invalid escape sequence '\\s'`——冒烟输出里就夹着它，
    而那正是刚用行缓冲修干净的那条通道。

    这类污染不会让任何测试变红，所以要专门钉一条。
    """

    def test_源码编译没有任何警告(self):
        src = pathlib.Path(ca.__file__).read_text()
        self.assertIn("SyntaxWarning" if False else "def ", src, "前提不成立：读到的不是源码")
        with warnings.catch_warnings(record=True) as got:
            warnings.simplefilter("always")
            compile(src, ca.__file__, "exec")
        self.assertEqual([f"{w.category.__name__}: {w.message}" for w in got], [])


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

    def test_混进来的ROUND_MARK源码行不许被当成新一轮的开始(self):
        # status 是外部观察者，它只能看最后一轮。子串搜索会把日志里转述的那行
        # 源码当成新一轮的开始——本轮的打断标记被甩到「上一轮」去，
        # status 于是把一轮被打断的运行报成 failed。
        源码行 = 'ROUND_MARK = "===== codex-agent "   # 每轮开跑前写进日志的分隔符前缀'
        self.assertIn(ca.ROUND_MARK, 源码行, "前提不成立：样本行里没有分隔符前缀")
        log = self._log(ca.round_separator("run", "t", "2026-09-19T10:00:00") + "\n"
                        + ca.INTERRUPT_MARK + "\n" + 源码行 + "\n")
        self.assertTrue(ca.has_interrupt_mark(ca.read_last_round(log)),
                        "最后一轮被那行源码切断了，打断标记被甩掉")

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
            text = ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"]).text
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
            rd = ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertIn(ca.INTERRUPT_MARK, log.read_text(), "前提不成立：打断标记没写进日志")

        self.assertEqual(ca.judge(rd).state, "interrupted")
        # 反面钉一条：猜边界（只看最后一轮）在这里当场失明——证明上面那条真的在挡东西
        self.assertNotEqual(ca.judge(ca.Round(report, ca.read_last_round(log))).state,
                            "interrupted")

    def test_区间右端截到下一个分隔符_后一轮的内容不许被吞进前一轮(self):
        d = self._home()
        log = ca._log_path(d, "t")
        with _no_codex() as popen:
            popen.side_effect = self._spawn_writing(
                log, "本轮干净\n",
                ca.round_separator("resume", "t", "2026-09-19T00:00:01") + "\n",
                ERR_FATAL + "\n")
            text = ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"]).text
        self.assertEqual(text, "本轮干净\n")
        self.assertEqual(ca.runtime_error_lines(text), [],
                         "后一轮的致命错误被算到了前一轮头上")

    def test_日志里混进ROUND_MARK的源码行_不许被当成轮次分隔符(self):
        # 这个仓库的日常就是派 codex 改 codex_agent.py 自己，源码行进日志是常态；
        # 模块 docstring 也写着「日志里还混着 brief 原文和 codex 转述的子进程输出」。
        # 子串搜索会在这里切断本轮：切剩 'ROUND_MARK = "' 14 个字符，
        # 打断标记被甩到本轮之外，judge 从 interrupted 翻成 failed。
        d = self._home()
        log = ca._log_path(d, "t")
        源码行 = 'ROUND_MARK = "===== codex-agent "   # 每轮开跑前写进日志的分隔符前缀'
        self.assertIn(ca.ROUND_MARK, 源码行, "前提不成立：样本行里没有分隔符前缀")
        with _no_codex() as popen:
            popen.side_effect = self._spawn_writing(
                log, 源码行 + "\n", ca.INTERRUPT_MARK + "\n")
            rd = ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertIn(源码行, rd.text, "本轮文本被那行源码切断了")
        self.assertEqual(ca.judge(rd).state, "interrupted")

    def test_日志里混进INTERRUPT_MARK的源码行_不许被当成真打断(self):
        # 反方向：一轮真正失败的运行，日志里恰好转述了那行常量定义。
        # 子串搜索会判成 interrupted、退出码 130，而照契约做决定的 agent
        # 会去 resume 一个根本没被打断的失败轮。
        d = self._home()
        log = ca._log_path(d, "t")
        源码行 = f'INTERRUPT_MARK = "{ca.INTERRUPT_MARK}"'
        self.assertIn(ca.INTERRUPT_MARK, 源码行, "前提不成立：样本行里没有打断标记")
        with _no_codex() as popen:
            popen.side_effect = self._spawn_writing(log, 源码行 + "\n")
            rd = ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertEqual(ca.judge(rd).state, "failed")

    def test_codex最后一块输出没有换行时_痕迹仍然落在行首(self):
        """整行匹配给自己引入了一条新前提：**这行必须落在行首**。而没有任何一方
        保证它——痕迹由 interrupt_codex 用 O_APPEND 追在日志末尾，日志末尾就是
        `_tee_until_exit` 最后一次 `log.write(chunk)` 留下的，而 **read1(1024) 的
        边界是任意的**：codex 流式输出被 INT 截在半行是**常态，不是边角**。

        接在半行后面的痕迹，整行匹配当场认不出，judge 从 interrupted(130) 退回
        failed(1)——正是这整轮改动要消灭的那个 bug，绕了一圈从第三层回来。
        修法与本轮设计原则同源：**写者保证，不是读者猜。**
        """
        d = self._home()
        log = ca._log_path(d, "t")
        半行 = "codex 正输出到一半就被打断"
        self.assertFalse(半行.endswith("\n"), "前提不成立：这块输出有换行结尾，测不到半行")

        def spawn(*a, **k):
            with open(log, "ab") as f:
                f.write(半行.encode())          # 最后一块输出没有换行结尾
            with mock.patch.object(ca.os, "kill"):
                ca.interrupt_codex(4242, log, "interrupt-and-resume")
            return mock.DEFAULT

        with _no_codex() as popen:
            popen.side_effect = spawn
            rd = ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertTrue(ca.has_interrupt_mark(rd.text),
                        "痕迹被接在半行后面，整行匹配认不出它")
        self.assertEqual(ca.judge(rd).state, "interrupted")
        self.assertEqual(ca.EXIT[ca.judge(rd).state], 130)

    def test_上一轮尾巴没有换行时_下一轮分隔符仍然落在行首(self):
        # 同一条前提的另一个方向：分隔符接在上一轮的半行后面，_ROUND_LINE 认不出，
        # read_last_round 就把两轮连成一轮——上一轮的错误算到这一轮头上。
        d = self._home()
        log = ca._log_path(d, "t")
        尾巴 = "上一轮被截在半行"
        self.assertFalse(尾巴.endswith("\n"), "前提不成立：尾巴有换行，测不到")
        log.write_bytes(尾巴.encode())
        with _no_codex() as popen:
            popen.side_effect = self._spawn_writing(log, "本轮的内容\n")
            ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertNotIn(尾巴, ca.read_last_round(log),
                         "分隔符没落在行首，上一轮的尾巴被算进了这一轮")

    def test_切的是字节不是字符_中文日志不许错位(self):
        # 日志里全是中文：brief 原文、codex 的中文输出。按字符切会整体错位，
        # 切出来的开头是半截字节——判据读到的「本轮」根本不是本轮。
        d = self._home()
        log = ca._log_path(d, "t")
        log.write_bytes("上一轮写了很多中文内容\n".encode())
        with _no_codex() as popen:
            popen.side_effect = self._spawn_writing(log, "本轮第一行\n")
            text = ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"]).text
        raw = log.read_bytes()
        self.assertNotEqual(len(raw), len(raw.decode()),
                            "前提不成立：日志里没有多字节字符，这条测不到错位")
        self.assertEqual(text, "本轮第一行\n")

    def test_非法kind当场被挡住_而且什么副作用都没产生(self):
        # 校验排在 write_meta／clear_report／spawn **全部之前**：
        # 任何一件先发生，失败就会留下半个状态。
        d = self._home()
        for bad in ("interrupt and resume", "run\nx", None, "", "瞎写的"):
            with self.subTest(bad=bad):
                with _no_codex() as popen:
                    with self.assertRaises(ValueError):
                        ca.run_codex(bad, d, "t", _full_meta("t"), lambda r: ["codex"])
                    popen.assert_not_called()
                self.assertFalse(ca.meta_path(d, "t").exists(), "元数据已经落盘了")
                self.assertFalse(ca._log_path(d, "t").exists(), "日志已经写了")

    def test_非法kind若被放过会怎样_这是上面那条为什么承重(self):
        """kind 带空格 → `_ROUND_LINE` 整行认不出 → 两轮被并成一轮，
        上一轮的 `Error:` 算进本轮。

        `_ROUND_LINE` 那段注释说「三段都不含空格」，而任务名由 _TASK_NAME 管着、
        时间戳由 isoformat 结构保证——**只有 kind 是空头支票**，所以它必须是枚举。
        """
        p = pathlib.Path(tempfile.mkdtemp()) / "t.log"
        坏分隔符 = ca.round_separator("interrupt and resume", "t", "2026-09-19T00:00:01")
        p.write_text(ca.round_separator("run", "t", "2026-09-19T00:00:00") + "\n"
                     + ERR_FATAL + "\n" + 坏分隔符 + "\n干净收尾\n")
        self.assertEqual(len(ca.runtime_error_lines(ca.read_last_round(p))), 1,
                         "前提变了：带空格的分隔符居然被认出来了，那 kind 就不用是枚举了")

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
    所以**四条路各钉一条**：run 和 resume 用 run_codex 回传的本轮文本、
    status 不在跑时用最后一轮、status **还在跑时根本不读日志**
    （judge 那一支用不到它，而 status 是轮询用的热路径）。
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
             "--effort", "low", "--account", "default", "--no-skill"])
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
            ["resume", "t", "--brief", str(self.brief), "--effort", "low", "--no-skill"])
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

        def spy(rd):
            texts.append(rd.text)
            v = real_judge(rd)
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
        return ca.judge(ca.Round(self.report, round_text))

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
        v = ca.judge(ca.Round(self.report, ca.INTERRUPT_MARK + "\n"))
        self.assertEqual(v.state, "interrupted")
        self.assertEqual(ca.EXIT[v.state], 130)
        self.assertIn("resume", v.reason)

    def test_额度上限和写锁仍然是failed_它们resume救不回来(self):
        # 这两条的补救是换账号／新起任务，不是 resume——不许被第五态顺手吃掉
        for mark in (ca.USAGE_LIMIT_MARK, ca.THREAD_LOCK_MARK):
            with self.subTest(mark=mark):
                v = ca.judge(ca.Round(self.report, mark + "\n" + ca.INTERRUPT_MARK + "\n"))
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
        v = ca.judge(ca.Round(self.report, ERR_ROLLOUT_ON_INTERRUPT + "\n" + ca.INTERRUPT_MARK + "\n"))
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

    def test_账号目录名含空白或控制字符就拒跑_并点名是哪个目录(self):
        # 这个名字会进 status 的第二列，而那一列是按空白切分的边界之一。
        # 约束必须在**入口**：扫描是它进入系统的唯一入口。
        for bad in ("bad acct", "bad\tacct", "bad\nacct"):
            with self.subTest(bad=bad):
                d = self.home / ".codex-accounts" / bad
                d.mkdir()
                try:
                    self.assertTrue(d.is_dir(), "前提不成立：这种名字的目录建不起来，那就没有洞")
                    with self.assertRaises(ca.Rejected) as cm:
                        ca.account_choices()
                    self.assertIn(bad, cm.exception.message)
                finally:
                    d.rmdir()
        self.assertEqual(ca.account_choices(), ["default", "acct2"],
                         "前提不成立：坏目录清掉之后它就该正常返回")

    def test_扫描期的拒绝也要说人话_不能只剩一个光秃秃的退出码2(self):
        # account_choices() 在 build_parser() 里被调用，而那一句排在 main() 的
        # try 之外的话，Rejected 会直接当 SystemExit(2) 逃出去——message 全丢，
        # 调用方拿到一个没有任何解释的 2。这是 agent 最救不回来的一种失败。
        (self.home / ".codex-accounts" / "bad acct").mkdir()
        err = io.StringIO()
        with mock.patch.object(ca.sys, "stdout", mock.MagicMock()), \
             mock.patch.object(ca.sys, "argv", ["codex-agent", "status"]), \
             contextlib.redirect_stderr(err):
            self.assertEqual(ca.main(), 2)
        self.assertIn("bad acct", err.getvalue())

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


class TestSkillGuardIsDerivedPerRound(unittest.TestCase):
    """兜底句**每轮派生**，且**只能由工具写**。

    旧常量那半句「除非本 brief 明确指定」本来就是「CLI 没有这个参数」的变通：
    调用方无处声明白名单，只好让 brief 正文去破例。--skill 出现之后那半句就该
    消失——白名单由 CLI 指定，brief 正文不再是声明渠道。

    断言的是**字面量**，不是 ca.SKILL_GUARD_STEM 拼出来的串：后者两边会一起动，
    把措辞整个改掉测试照样绿（本仓在这上面栽过五次）。
    """

    def test_无白名单时的措辞(self):
        self.assertEqual(ca.build_skill_guard(()), "**不得使用任何 skill。**")

    def test_有白名单时逐行列出每条绝对路径_并且带上去读的指令(self):
        """**许可不等于指令**——这是立项要杀的那类失效换了个形状。

        只说「这几个除外」，codex 拿到的是「你可以用」，没有任何一句让它去读。
        不读 → 日志里就没有那行 `cat <路径>` → judge 结构上看不见 → 照报 success，
        正是 34.9 秒那次的同一种失效。
        而「不把 skill 软链进隔离目录」的全部论证都架在这行 `cat` 上
        （见 _add_prompt_round_args），指令没了那条论证也一起塌。
        """
        self.assertEqual(
            ca.build_skill_guard(("/abs/one/SKILL.md", "/abs/two/SKILL.md")),
            "**不得使用任何 skill，以下几个除外（动手前先逐个读一遍）：**\n"
            "- /abs/one/SKILL.md\n"
            "- /abs/two/SKILL.md")

    def test_无白名单时不带这条指令_没东西可读(self):
        # 正面控制：指令只在有白名单那一支出现。少了它，一个「两支都加指令」的
        # 实现也全绿，而那句话在无白名单时是句废话（让它去读一个空清单）。
        self.assertNotIn("读一遍", ca.build_skill_guard(()))

    def test_白名单必须是tuple_裸str会被逐字符拆开(self):
        """`args.skills` 恒为 tuple 这个不变量**只到 parser 为止**，parser 之下
        本来一道闸都没有。实测那两种坏法**全是静默的**：

            build_skill_guard("/abs/SKILL.md") → 拼出 14 行：`- /`、`- a`、`- b`…
            build_skill_guard(None)            → 返回「无白名单」那句，白名单被无声吞掉

        仓内三个调用点都传 args.skills 所以现在不可达——但 kind／cause／state
        当初上 _require_enum 时也一样不可达。散文不是约束。
        """
        for bad in ("/abs/SKILL.md", None, ["/abs/SKILL.md"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as cm:
                    ca.build_skill_guard(bad)
                self.assertIn("skills", str(cm.exception))

    def test_白名单的每一项必须是路径字符串(self):
        # Path 对象在兜底句里印出来一模一样，却会让 write_meta 的 json.dumps
        # 在很久以后才炸；整数更坏，json 收得下，静默落盘。
        for bad in ((pathlib.Path("/abs/SKILL.md"),), (1,)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ca.build_skill_guard(bad)

    def test_空tuple和正常tuple都照收(self):
        # 正面控制：没有它，一个「什么都拒」的实现也全绿
        self.assertEqual(ca.build_skill_guard(()), "**不得使用任何 skill。**")
        self.assertIn("- /a/SKILL.md", ca.build_skill_guard(("/a/SKILL.md",)))

    def test_brief自带兜底句就拒跑_并提示改用参数(self):
        with self.assertRaises(ca.Rejected) as cm:
            ca.prepend_skill_guard("**不得使用任何 skill。**\n\n干活", ())
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--no-skill", cm.exception.message)
        self.assertIn("--skill", cm.exception.message)

    def test_旧那句兜底句也算自带_SKILL_md历史上印过它(self):
        # 调用方照抄 SKILL.md 开头那句是**可达路径**，不拒就会出现两句互相
        # 矛盾的兜底句。字面量写死那句旧话：它已经不在代码里了，只能这么钉。
        with self.assertRaises(ca.Rejected):
            ca.prepend_skill_guard("**不得使用任何 skill，除非本 brief 明确指定。**\n\n干活", ())

    def test_SKILL_GUARD常量已经不存在_它不再是每轮同一句话(self):
        self.assertFalse(hasattr(ca, "SKILL_GUARD"),
                         "常量留着就是第二个家：派生一份、常量一份，两份必然漂移")


class TestArgv(unittest.TestCase):
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


class TestWriteMetaIsAtomic(_HomeSandbox):
    """落元数据必须**原子替换**，不许原地截断重写。

    `Path.write_text` 先把目标截成 0 再往里填，而 `status` 随时可能在另一个
    进程里读同一份 json——`all_metas`／`find_meta` 都是裸 `json.loads`。
    2026-09-20 实测（截断重写的版本）：1 秒里写 5289 次、并发读 12284 次，
    其中 **8277 次读到半截 json**。撞上的调用方拿到的是一个裸
    `JSONDecodeError` traceback，不是干净的护栏拒绝——正是本工具存在的理由
    反过来。这个 bug 是被那条真进程信号测试偶发地照出来的：它轮询
    `tasks/t.json` 等 session_id 落盘，而包装器正好在写。
    """

    def test_并发读永远读不到半截json(self):
        d = ca.ensure_isolation("default")
        meta = _full_meta("t", session_id="01a0b408-f718-7ff3-8123-d5202551acba")
        ca.write_meta(d, "t", meta)
        q = ca.meta_path(d, "t")
        stop, bad, good = [False], [0], [0]

        def reader():
            while not stop[0]:
                try:
                    json.loads(q.read_text())
                    good[0] += 1
                except json.JSONDecodeError:
                    bad[0] += 1
                except FileNotFoundError:
                    bad[0] += 1      # 目标短暂消失也算坏：status 会当成「没这个任务」

        th = threading.Thread(target=reader)
        th.start()
        try:
            deadline = time.time() + 0.3
            while time.time() < deadline:
                ca.write_meta(d, "t", meta)
        finally:
            stop[0] = True
            th.join()
        self.assertGreater(good[0], 100,
                           f"前提不成立：读者根本没跑起来（成功读 {good[0]} 次），这条测不到并发")
        self.assertEqual(bad[0], 0, f"{bad[0]} 次读到半截或消失的 json——写不是原子的")

    def test_崩在替换之前留下的残片不会被当成任务(self):
        """残片得由 `write_meta` **自己**造出来，不能手写一个名字去测。

        手写 `t.json.tmp` 再断言 all_metas 忽略它，测的是那个手写的名字；
        实现把临时名改成 `t.tmp.json`（会被 `*.json` 扫进去）照样全绿——
        实测这个突变**存活**过。这里改成让 os.replace 崩掉，残片就是实现
        真正用的那个名字。
        """
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "good", _full_meta("good"))
        tasks = ca.meta_path(d, "good").parent
        before = {q.name for q in tasks.iterdir()}
        with mock.patch.object(ca.os, "replace", side_effect=OSError("崩在替换之前")):
            with self.assertRaises(OSError):
                ca.write_meta(d, "t", _full_meta("t"))
        leftovers = {q.name for q in tasks.iterdir()} - before
        self.assertTrue(leftovers, "前提不成立：没留下残片，这条测不到任何东西")
        # 残片要是被 *.json 扫进去，它自己就成了一份「缺字段的坏元数据」，
        # 而那条的爆炸半径是整个 status 列表（见 REQUIRED_META_KEYS 上方）
        self.assertEqual([m["task"] for _, m in ca.all_metas()], ["good"],
                         f"残片 {leftovers} 被当成任务扫进来了")


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
    FIELDS = {"task", "account", "dir", "effort", "skills", "session_id", "started_at",
              "writer_pid", "writer_start"}

    def test_字段清单的绝对值(self):
        self.assertEqual(set(ca.REQUIRED_META_KEYS), self.FIELDS)
        # resume 要回到同一个目录、同一个会话，这两个字段是它的命根子
        self.assertIn("dir", self.FIELDS)
        self.assertIn("session_id", self.FIELDS)
        # **codex** 的存活必须每次现查：存下来的 PID 会过期、会被系统复用，
        # 而且它有现场特征可查（comm=codex + argv 里的报告路径），见 find_codex_pid。
        self.assertNotIn("pid", self.FIELDS)
        # **writer 是另一回事**，所以它有自己的名字：写日志的是包装器自己，
        # 「它还在不在写」没有任何现场特征。身份**成对**存——只存 PID 就退回
        # 上面那条防的老毛病了。
        self.assertIn("writer_pid", self.FIELDS)
        self.assertIn("writer_start", self.FIELDS)
        # 白名单是「最后一次调用给了什么」，要审计就读这个字段——它刻意不进
        # status 的列：skill 路径是任意长度的绝对路径，进数据行会把格式撑坏。
        self.assertIn("skills", self.FIELDS)

    def test_构造器自己就把写者记进去(self):
        """`new_meta` 产出的是「一份此刻开始的任务的完整记录」，**写者是谁是这份
        记录的一部分**，所以它自己去拿，不收参数。

        收参数的那版实测是**不可观测**的：`run_codex` 在 write_meta 前会盖一次，
        传垃圾进来落盘的仍是真身份——于是「传反了」（两个都是 str，starttime
        长得就像个 pid）永远没人发现。不可观测的参数就是给误用留的口子。
        """
        meta = ca.new_meta("t", "default", "/abs/x", "low", ())
        self.assertEqual((meta["writer_pid"], meta["writer_start"]), ca._writer_identity())

    def test_构造器的键集就是校验面(self):
        self.assertEqual(set(ca.new_meta("t", "default", "/abs/x", "low", ())),
                         set(ca.REQUIRED_META_KEYS))

    def test_run落盘的元数据键集与校验面相等_不多不少(self):
        # 相等而不是包含：少一个字段任务就够不着了；多塞一个 **codex 的** pid 又会
        # 破坏「codex 存活必须每次现查」那条设计意图（存下来的 PID 会过期、会被复用）。
        # writer 的那一对是例外，理由见 test_字段清单的绝对值。
        d = ca.ensure_isolation("default")
        args = ca.build_parser().parse_args(
            ["run", "--task", "t", "--dir", str(self.workdir), "--brief", str(self.brief),
             "--effort", "low", "--account", "default", "--no-skill"])
        with _no_codex():
            ca.cmd_run(args)
        self.assertEqual(set(json.loads(ca.meta_path(d, "t").read_text())),
                         set(ca.REQUIRED_META_KEYS))

    def test_new_meta也守同一道_它是白名单的第二个家(self):
        # 两个家各守一道：build_skill_guard 管送进 codex 的那份，new_meta 管落盘
        # 的那份。_resume_round 今天恰好先调前者，但闸不能靠调用顺序站着。
        for bad in ("/abs/SKILL.md", None, ["/abs/SKILL.md"], (1,)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ca.new_meta("t", "default", "/abs/x", "low", bad)
        self.assertEqual(ca.new_meta("t", "default", "/abs/x", "low", ())["skills"], ())

    def test_run落盘的writer身份就是本进程(self):
        d = ca.ensure_isolation("default")
        args = ca.build_parser().parse_args(
            ["run", "--task", "t", "--dir", str(self.workdir), "--brief", str(self.brief),
             "--effort", "low", "--account", "default", "--no-skill"])
        with _no_codex():
            ca.cmd_run(args)
        落盘 = json.loads(ca.meta_path(d, "t").read_text())
        self.assertEqual((落盘["writer_pid"], 落盘["writer_start"]), ca._writer_identity())

    def test_每一轮都重打writer身份_resume不沿用上一轮的(self):
        """resume 那一轮的 writer 是**另一个进程**，上一轮那个早就死了。

        `_resume_round` 不经过 `new_meta`，它手里那份 meta 是 `_load_meta` 从磁盘
        读回来的——沿用的话，下一条命令一问「上一轮那个进程还在吗」，答的是**上上轮**
        那个进程的事。所以盖章的地方必须是 `run_codex`（三条路的唯一交汇点）。
        """
        d = ca.ensure_isolation("default")
        with _no_codex():
            ca.run_codex("run", d, "t", _full_meta("t"), lambda r: ["codex"])
        落盘 = json.loads(ca.meta_path(d, "t").read_text())
        self.assertNotEqual(落盘["writer_pid"], GONE_PID, "沿用了传进来那份的陈旧身份")
        self.assertEqual((落盘["writer_pid"], 落盘["writer_start"]), ca._writer_identity())

    def test_run落盘的skills就是命令行给的那几条(self):
        d = ca.ensure_isolation("default")
        skill = self.home / "tdd_SKILL.md"
        skill.write_text("---\nname: tdd\n---\n")
        args = ca.build_parser().parse_args(
            ["run", "--task", "t", "--dir", str(self.workdir), "--brief", str(self.brief),
             "--effort", "low", "--account", "default", "--skill", str(skill)])
        with _no_codex():
            ca.cmd_run(args)
        self.assertEqual(json.loads(ca.meta_path(d, "t").read_text())["skills"], [str(skill)])

    def test_resume刷新skills_元数据描述的是最后一次调用(self):
        d = ca.ensure_isolation("default")
        first, second = self.home / "a_SKILL.md", self.home / "b_SKILL.md"
        for q in (first, second):
            q.write_text("---\nname: x\n---\n")
        ca.write_meta(d, "t", _full_meta("t", session_id="s1", dir=str(self.workdir),
                                         skills=[str(first)]))
        args = ca.build_parser().parse_args(
            ["resume", "t", "--brief", str(self.brief), "--effort", "high",
             "--skill", str(second)])
        with _no_codex(), mock.patch.object(ca, "find_codex_pid", return_value=None):
            ca.cmd_resume(args)
        meta = json.loads(ca.meta_path(d, "t").read_text())
        self.assertEqual(meta["skills"], [str(second)], "skills 没跟着 effort 一起刷新")
        self.assertEqual(meta["effort"], "high", "前提不成立：effort 本来就该刷新")

    def test_interrupt_and_resume也把skills接了进去(self):
        # _resume_round 是两条路共用的，但接线是各自的：这条命令传成 [] 或漏传，
        # 上面那条测试一个字都测不出来。
        d = ca.ensure_isolation("default")
        skill = self.home / "c_SKILL.md"
        skill.write_text("---\nname: x\n---\n")
        ca.write_meta(d, "t", _full_meta("t", session_id="s1", dir=str(self.workdir)))
        args = ca.build_parser().parse_args(
            ["interrupt-and-resume", "t", "--brief", str(self.brief), "--effort", "low",
             "--skill", str(skill)])
        with _no_codex(), mock.patch.object(ca, "find_codex_pid", return_value=None):
            ca.cmd_interrupt_and_resume(args)
        self.assertEqual(json.loads(ca.meta_path(d, "t").read_text())["skills"], [str(skill)])


class TestProcessIdentity(unittest.TestCase):
    """进程身份 = PID + `/proc/<pid>/stat` 第 22 字段（启动时刻）。

    裸 PID 判不了 PID 复用：同一个 PID 换了个进程，`os.kill(pid, 0)` 一样说「活着」。
    """

    def test_身份成对返回_两半都是str(self):
        # 两半必须同时拿到：分开取就会出现「记了 PID 没记启动时刻」的半截身份，
        # 而半截身份等于退回裸 PID。
        # 两半都是 str，是因为**写入和比对共用这同一个产生点**——类型不匹配
        # 从构造上就无从发生，所以 _load_meta 不必给这两个字段加非对称的类型校验。
        ident = ca._writer_identity()
        self.assertEqual(len(ident), 2)
        self.assertEqual(ident[0], str(os.getpid()))
        self.assertTrue(all(isinstance(v, str) for v in ident),
                        f"身份必须是 str：{ident!r}——比对是字符串比对，int 会把活着的任务判死")

    def test_启动时刻取的是stat的第22字段(self):
        """判据是**独立算出来的**，不是拿被测函数自己的切法当参照。

        第 22 字段是「开机以来的滴答数」，所以「进程年龄 = 开机至今 − 启动时刻」
        必须落在一个很小的正数区间里。左右两个邻居都被这条挡住：
        第 21 字段 itrealvalue 恒为 0（年龄 = 整个开机时长），
        第 23 字段 vsize 是字节数（2026-09-20 实测 18,898,944，年龄算出来是几万秒）。
        """
        滴答每秒 = os.sysconf("SC_CLK_TCK")
        开机至今 = int(float(pathlib.Path("/proc/uptime").read_text().split()[0]) * 滴答每秒)
        启动时刻 = int(ca._writer_identity()[1])
        self.assertGreater(启动时刻, 0, "第 21 字段(itrealvalue)恒为 0——下标写小了")
        年龄 = 开机至今 - 启动时刻
        self.assertGreaterEqual(年龄, 0, "启动时刻比开机至今还晚，切到别的字段上去了")
        self.assertLess(年龄, 300 * 滴答每秒,
                        "本测试进程不可能活了 5 分钟以上（整套测试实跑 ~5 秒）——下标写大了")

    def test_comm含空格括号制表符和裸换行时仍然解析正确(self):
        """`comm` 是进程自己用 `prctl(PR_SET_NAME)` 设的**任意 15 字节**。

        2026-09-20 实测 comm = `we ird)\\nx` 时 `/proc/<pid>/stat` **按行读会得到 2 行**，
        而第一行里最后一个 `)` 落在 comm **内部**——按行读切出来的字段列表长度是 **0**，
        下标 19 当场 IndexError；就算侥幸不炸，切出来的「启动时刻」也是 comm 的碎片。
        这种瞎法很安静：只在别人给进程改过名的时候才发作。
        参照物是**改名之前**读到的启动时刻，由独立的一次读取得到。

        **刻意只比启动时刻，不比运行状态。** 状态是真会变的（陪练刚写完 `after`
        正要进 `sleep`，读到 R 还是 S 全看撞上哪一刻）——2026-09-20 实跑撞到过
        一次，整条测试于是变成随机红。状态那一半改钉「长度是 1」：/proc 的
        state 字段恒为单个字符，而切错了拿到的是 comm 的碎片（`raw.index` 那个
        突变切出来的是 `x)`），照样红。
        """
        陪练 = subprocess.Popen(
            [sys.executable, "-c",
             "import ctypes, sys, time\n"
             "sys.stdout.write('before\\n'); sys.stdout.flush()\n"
             "sys.stdin.readline()\n"
             "ctypes.CDLL('libc.so.6').prctl(15, b'we ird)\\nx', 0, 0, 0)\n"
             "sys.stdout.write('after\\n'); sys.stdout.flush()\n"
             "time.sleep(30)\n"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertEqual(陪练.stdout.readline(), b"before\n", "前提不成立：陪练没起来")
            改名前 = ca._read_stat_fields(str(陪练.pid))
            self.assertIsNotNone(改名前, "前提不成立：陪练的 /proc 读不到")
            陪练.stdin.write(b"go\n")
            陪练.stdin.flush()
            self.assertEqual(陪练.stdout.readline(), b"after\n", "前提不成立：陪练没改成名")
            raw = pathlib.Path(f"/proc/{陪练.pid}/stat").read_bytes()
            self.assertIn(b"\n", raw[:raw.rindex(b")")],
                          "前提不成立：comm 里没有裸换行，这条测不到「不许按行读」")
            self.assertEqual(len(raw.splitlines()), 2,
                             "前提不成立：这个文件按行读只有一行，那按行读的突变杀不掉")
            改名后 = ca._read_stat_fields(str(陪练.pid))
            self.assertEqual(改名后[1], 改名前[1],
                             "comm 里的空格/括号/制表符/裸换行把启动时刻切偏了")
            self.assertEqual(len(改名后[0]), 1,
                             f"切出来的不是运行状态而是 comm 的碎片：{改名后[0]!r}")
        finally:
            陪练.kill()
            陪练.wait()
            陪练.stdin.close()
            陪练.stdout.close()

    def test_进程不在了返回None(self):
        self.assertIsNone(ca._read_stat_fields(GONE_PID))


class TestPreviousWriterAlive(_HomeSandbox):
    """「上一轮那个进程还在吗」——名字就是它答的那个问题。

    **它不答「还有人写日志吗」**：那是它被用来回答的问题，不是它知道的事实。
    只有 `_wait_previous_round_ends` 一个调用点，所以设为私有，且收 `(home, task)`
    ——收 dict 就可以传错任务，而传错了它会一声不吭地答「停了」。
    """

    def setUp(self):
        super().setUp()
        self.d = ca.ensure_isolation("default")

    def _写盘(self, **over):
        ca.write_meta(self.d, "t", _full_meta("t", **over))

    def test_没有上一轮就判停了(self):
        self.assertFalse(ca.meta_path(self.d, "t").exists(), "前提不成立：元数据居然已经在了")
        self.assertFalse(ca._previous_writer_alive(self.d, "t"))

    def test_上一轮的writer还活着就判还在(self):
        # 反面也要有：一个永远返回 False 的实现也能让上面那条全绿。
        self._写盘(**_live_writer(self))
        self.assertTrue(ca._previous_writer_alive(self.d, "t"))

    def test_同一个PID但启动时刻对不上就判停了_防PID复用(self):
        # 自然情况下撞同一副 (PID, 10ms 刻度) 的余量是 445,255 倍，造不出来也不该造。
        # 但**比对逻辑本身**两行就测完了，而它正是那道防线。
        陪练 = _live_writer(self)
        self._写盘(**陪练)
        self.assertTrue(ca._previous_writer_alive(self.d, "t"), "前提不成立：陪练居然不算活着")
        self._写盘(writer_pid=陪练["writer_pid"],
                   writer_start=str(int(陪练["writer_start"]) + 1))
        self.assertFalse(ca._previous_writer_alive(self.d, "t"))

    def test_进程不在了就判停了(self):
        self._写盘(writer_pid=GONE_PID, writer_start=GONE_START)
        self.assertFalse(ca._previous_writer_alive(self.d, "t"))

    def test_元数据记的是本进程时判停了_否则同一进程连跑两轮会等自己(self):
        """**本进程不算「上一轮的 writer」**，而这一条得是代码不是注释。

        写日志的包装器就是跑 `run_codex` 的那个进程（tee 循环在它自己身上），
        所以它一落盘，磁盘上那副身份就是**它自己**。同一个进程再进一次
        `run_codex`，按「进程还在吗」直问就恒答「还在」——它在等自己，等到超时
        为止。失败模式是**60 秒静默挂起**（等待排在分隔符之前，屏幕和日志都是
        死的），而「别让调用方遇到静默挂起」正是本工具存在的理由，所以这条
        必须编进代码里，不能只写在注释里指望下一个人记得。
        """
        本进程 = dict(zip(("writer_pid", "writer_start"), ca._writer_identity()))
        # 前提用 _read_stat_fields 表达，**不碰谓词本身**：谓词正是突变要改的那个。
        self.assertIsNotNone(ca._read_stat_fields(本进程["writer_pid"]),
                             "前提不成立：本进程居然读不到自己，那这条测的是另一条分支")
        self._写盘(**本进程)
        self.assertFalse(ca._previous_writer_alive(self.d, "t"))

    def test_僵尸writer判停了_它的starttime和存活期完全一样(self):
        """**这条钉的是承重梁，不是锦上添花。**

        僵尸已经不执行任何代码、fd 全关，日志不可能再长。而它骗得过另外两道：
        `starttime` 和存活期**完全相同**（本测试当场断言这一点），`os.kill(pid,0)`
        也照样「成功」。去掉 `state == "Z"` 那一行，等的就变成「等它被父进程回收」
        ——那归 harness 的 bash/node 管，是第三方，正是 v4 被否掉的那条前提。

        这条测试里**不许再建第二个 `subprocess.Popen`**（见 `_wait_zombie`）。
        """
        僵尸 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.05)"])
        try:
            存活期 = None
            deadline = time.time() + 5
            while time.time() < deadline:
                fields = ca._read_stat_fields(str(僵尸.pid))
                if fields is None:
                    break
                if fields[0] == "Z":
                    僵尸期 = fields[1]
                    break
                存活期 = fields[1]
                time.sleep(0.005)
            else:
                self.fail(f"陪练 {僵尸.pid} 始终没变成僵尸，本测试验证不了任何东西")
            self.assertIsNotNone(存活期, "前提不成立：没在存活期读到过，比不了")
            self.assertEqual(僵尸期, 存活期,
                             "前提不成立：僵尸期的 starttime 居然变了——那这一行就不承重了")
            os.kill(僵尸.pid, 0)   # 前提：裸 PID 对僵尸「成功」；抛异常就是前提变了
            self._写盘(writer_pid=str(僵尸.pid), writer_start=僵尸期)
            self.assertFalse(ca._previous_writer_alive(self.d, "t"))
        finally:
            僵尸.wait()


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
        fake = _fake_codex(tmp)
        (tmp / "reports").mkdir()
        mark = str(tmp / "reports" / "t.md")
        pathlib.Path(mark).write_text("")
        proc = subprocess.Popen([str(fake), "-f", mark], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_argv(self, proc, mark)
            self.assertEqual(ca.find_codex_pid(mark), proc.pid)   # 前提：本来找得到
            with mock.patch.object(ca.os, "getuid", return_value=os.getuid() + 12345):
                self.assertIsNone(ca.find_codex_pid(mark))
        finally:
            proc.kill()
            proc.wait()

    def test_只认comm是codex的进程_别的进程不算(self):
        # 2026-09-19 实测：按报告路径反查会命中发命令的 bash 自己（comm=bash），
        # comm 过滤是承重的，不是保险。
        # 陪练进程把报告路径作为 argv 里**独立一项**传进去，和 codex 的 `-o <路径>`
        # 形状一致——否则测到的只是「没匹配上」，不是「comm 把它挡住了」。
        mark = "/tmp/codex-agent-selftest-不存在的报告.md"
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", mark])
        try:
            _wait_argv(self, proc, mark)
            self.assertIsNone(ca.find_codex_pid(mark))
        finally:
            proc.kill()
            proc.wait()

    def test_comm真是codex的进程会被找到_反向也要成立(self):
        # 只测"排除"的话，一个永远返回 None 的实现也能全绿。
        tmp = pathlib.Path(tempfile.mkdtemp())
        fake = _fake_codex(tmp)
        (tmp / "reports").mkdir()
        mark = str(tmp / "reports" / "t.md")
        pathlib.Path(mark).write_text("")
        proc = subprocess.Popen([str(fake), "-f", mark], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_argv(self, proc, mark)
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
        fake = _fake_codex(tmp)
        (tmp / "reports").mkdir()
        mine = str(tmp / "reports" / "mine.md")
        others = str(tmp / "reports" / "others.md")
        pathlib.Path(mine).write_text("")
        brief = f"请参考 {others} 里的结论再动手"      # 别的任务的报告路径只出现在正文里
        proc = subprocess.Popen([str(fake), "-f", mine, brief], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_argv(self, proc, brief)
            self.assertEqual(ca.find_codex_pid(mine), proc.pid)   # 前提：自己找得到自己
            self.assertIsNone(ca.find_codex_pid(others))
        finally:
            proc.kill()
            proc.wait()

    def test_僵尸codex不进候选集_argv过滤是承重的不是保险(self):
        """僵尸的 `cmdline` 是**空的**（2026-09-20 实测），所以 argv 元素比对
        自然把它挡在外面——`comm` 那时还是 `codex`，`pid_alive` 也还说「活着」。

        这条行为**没有名字**，却是「僵尸 codex 不该再收信号」的全部依靠，
        也是 `_wait_previous_round_ends` 里「只看 codex 会早放行」那句话的成因。
        补这条测试就是给它一个家。
        """
        tmp = pathlib.Path(tempfile.mkdtemp())
        fake = _fake_codex(tmp)
        (tmp / "reports").mkdir()
        mark = str(tmp / "reports" / "t.md")
        pathlib.Path(mark).write_text("")
        proc = subprocess.Popen([str(fake), "-f", mark], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_argv(self, proc, mark)
            self.assertEqual(ca.find_codex_pid(mark), proc.pid, "前提不成立：活着时就找不到它")
            proc.kill()
            _wait_zombie(self, proc.pid)
            self.assertEqual(pathlib.Path(f"/proc/{proc.pid}/comm").read_bytes().strip(), b"codex",
                             "前提不成立：僵尸的 comm 变了，那挡住它的就不是 argv 过滤")
            self.assertEqual(pathlib.Path(f"/proc/{proc.pid}/cmdline").read_bytes(), b"",
                             "前提不成立：僵尸的 cmdline 不空，这条测的就不是那件事")
            self.assertTrue(ca.pid_alive(proc.pid),
                            "前提不成立：裸 PID 对僵尸都说「死了」，那它本来就挡得住")
            self.assertIsNone(ca.find_codex_pid(mark))
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
        fake = _fake_codex(tmp)
        (tmp / "reports").mkdir()
        victim = str(tmp / "reports" / "aXmd.md")   # 任务 aXmd 的报告路径
        hunter = str(tmp / "reports" / "a.md")      # 任务 a 的报告路径
        pathlib.Path(victim).write_text("")
        proc = subprocess.Popen([str(fake), "-f", victim], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_argv(self, proc, victim)
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
        r"/proc|pgrep|pkill": "find_codex_pid / _previous_writer_alive",
        r"kill -INT|SIGTERM|SIGINT": "run_codex 的信号转发 + cmd_stop",
        r"mkdir -p": "ensure_isolation",
        r"--color": "build_run_argv（resume 不认它）",
        r"CODEX_HOME|CODEX_SQLITE_HOME": "codex_env",
        r"--sandbox|sandbox_mode": "build_run_argv / build_resume_argv",
        r"--disable|project_doc_max_bytes|approval_policy": "_COMMON",
        r"session id|session_id": "extract_session_id / 元数据",
        r"\bexit 1\b|退出码不可信": "judge（判据只看产物和日志）",
        r"不得使用任何 skill": "build_skill_guard（兜底句由 --skill/--no-skill 每轮派生）",
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
        # 退出码是对外契约，钉的是**数字和状态名的配对**，不是「这个数字出现过」。
        # 只钉出现过的话，把表改成 `1` interrupted / `130` failed（五个数字一个
        # 不少、含义全反）照样全绿——实测过。
        # 配对表从 EXIT 派生，不另写一份清单：两份清单必然漂移。
        for state, code in ca.EXIT.items():
            with self.subTest(state=state):
                self.assertRegex(skill, rf"`{code}`\s*{state}",
                                 f"SKILL.md 里 {code} 没有紧跟着 {state}")
        # USAGE_ERROR 不在 EXIT 表里（它不是判据结论），单独钉
        self.assertRegex(skill, r"`2`.*(参数|护栏)")
        # 「要不要为此打断」是判断力，代码替不了：它要知道这条信息值多少、
        # 在途工作损失多少，后者在 codex 里根本不可观测
        self.assertIn("值不值", skill)
        # 白名单是**对外契约**：给哪几个 skill 是判断力，代码替不了。
        # 钉的是**配对**，不是「这两个词出现过」——实测过那种写法是空的：
        # 把表格里「给它 skill」整行的两个参数删掉，`--no-skill` 在启动那行的
        # 示例命令里还在，`--skill` 在「外加 --skill/--no-skill 二选一」里也还在，
        # 于是删掉承重内容照样全绿（这正是本文件开头第 5 条踩过的坑）。
        # 两条各钉一个家，删任一处都红：
        self.assertRegex(skill, r"--skill[^\n]*绝对路径",
                         "「--skill 收的是 SKILL.md 的绝对路径」这条没了")
        self.assertRegex(skill, r"(--no-skill[^\n]*二选一|二选一[^\n]*--no-skill)",
                         "「--skill/--no-skill 二选一必填」这条契约没了")
        # status 列表形态的行结构是**接口事实**，不是机制泄漏（怎么切是调用方
        # 自己的事，文档不写 split）。它没人守的话整段删掉照样全绿——实测过。
        self.assertRegex(skill, r"一行一个任务", "status 是「一行一个任务」这条接口事实没了")
        self.assertRegex(skill, r"前四列.*任务名.*账号.*状态.*退出码",
                         "前四列是哪四列、什么顺序——这条没了，调用方就得自己猜")
        self.assertRegex(skill, r"缩进的行是明细", "「缩进的行不是任务」这条没了")


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


class TestSkillPathArg(unittest.TestCase):
    """`--skill` 的四条校验。**四条缺一不可**，每条各钉一条。

    为什么必须当场拒（而不是让 codex 自己去发现）：2026-09-19 真跑过一次
    brief 指向不存在的 SKILL.md，codex 第一步 `cat` 退 1，第二步拿 `find` 翻
    真实 home **跑了 34.9 秒**，结论是「未找到该文件，因此无法严格按其流程
    执行，尚未创建 out.txt」——磁盘上产物**不存在**，而本工具判 success、
    **退出码 0**。「零工作量」被报成「完成」，没有任何别的信号救得回来。
    """

    def _dir(self):
        return pathlib.Path(tempfile.mkdtemp())

    def _skill_file(self):
        d = self._dir()
        q = d / "SKILL.md"
        q.write_text("---\nname: x\n---\n")
        return q

    def test_收一个正常的绝对路径SKILL文件(self):
        q = self._skill_file()
        self.assertTrue(q.is_absolute() and q.is_file(), "前提不成立：样本文件没建起来")
        self.assertEqual(ca.skill_path(str(q)), str(q))

    def test_相对路径被拒(self):
        with self.assertRaises(argparse.ArgumentTypeError) as cm:
            ca.skill_path("skills/foo/SKILL.md")
        self.assertIn("绝对路径", str(cm.exception))

    def test_指向不存在的文件被拒(self):
        missing = self._dir() / "SKILL.md"
        self.assertFalse(missing.exists(), "前提不成立：这个文件居然存在")
        with self.assertRaises(argparse.ArgumentTypeError):
            ca.skill_path(str(missing))

    def test_权限000的文件被拒_is_file说True而codex的cat退1(self):
        # 这条是审查实测逼出来的：只查 is_absolute + is_file 的话，
        # 「文件不可读」这一支原样漏掉——而那正是核心承诺失效的地方。
        self.assertNotEqual(os.geteuid(), 0, "前提不成立：root 读得了 000 的文件，这条测不到")
        q = self._skill_file()
        os.chmod(q, 0o000)
        try:
            self.assertTrue(q.is_file(), "前提不成立：is_file 本来就该返回 True，这条才有意义")
            self.assertFalse(os.access(q, os.R_OK), "前提不成立：这个文件居然读得了")
            self.assertEqual(subprocess.run(["cat", str(q)], capture_output=True).returncode, 1,
                             "前提不成立：codex 用的 cat 居然没退 1")
            with self.assertRaises(argparse.ArgumentTypeError) as cm:
                ca.skill_path(str(q))
            self.assertIn("读不了", str(cm.exception))
        finally:
            os.chmod(q, 0o644)

    def test_传目录被拒_错误信息要说清期望的是文件本身(self):
        # 传 skill 目录是最容易犯的错，而 is_file() 为 False 时调用方看不出为什么
        d = self._dir()
        self.assertTrue(d.is_dir(), "前提不成立：目录没建起来")
        with self.assertRaises(argparse.ArgumentTypeError) as cm:
            ca.skill_path(str(d))
        self.assertIn("SKILL.md", str(cm.exception))

    def test_含控制字符的路径被拒(self):
        q = self._skill_file()
        for bad in (f"{q}\t", f"{q}\n", "/abs\x00/SKILL.md"):
            # assertRaises 必须嵌在 subTest **里面**：写成 `with subTest(), assertRaises()`
            # 再在块外读 cm.exception，一旦没抛出，真正的失败信息会被随后那句
            # `'_AssertRaisesContext' object has no attribute 'exception'` 盖掉。
            with self.subTest(bad=bad):
                with self.assertRaises(argparse.ArgumentTypeError) as cm:
                    ca.skill_path(bad)
                self.assertIn("控制字符", str(cm.exception))

    def test_收下什么就原样还什么_刻意不resolve(self):
        """这条是**可观测性论证的地基**，不是洁癖。

        命令行上写的、brief 白名单行里印的、日志里 `cat` 出现的必须是**同一个
        串**——人和 grep 才对得上「codex 到底读没读那一个」。规范化之后这三者
        就成了三个不同的串。
        用 `/./` 而不是靠 /tmp 是不是软链：那种样本在这台机器上恰好相等，
        `return str(q.resolve())` 的突变会**存活**（实测过）。
        """
        q = self._skill_file()
        weird = f"{q.parent}/./{q.name}"
        self.assertNotEqual(weird, str(q), "前提不成立：这个样本没制造出任何差别")
        self.assertTrue(pathlib.Path(weird).is_file(), "前提不成立：带 /./ 的路径打不开")
        self.assertEqual(ca.skill_path(weird), weird)

    def test_每条拒绝都点名是哪个路径(self):
        # 调用方是 agent：它拿到的只有 stderr 那一行，不点名就无从改起
        q = self._skill_file()
        os.chmod(q, 0o000)
        try:
            cases = ["skills/foo/SKILL.md", str(self._dir() / "SKILL.md"),
                     str(q.parent), str(q)]
            for bad in cases:
                with self.subTest(bad=bad):
                    with self.assertRaises(argparse.ArgumentTypeError) as cm:
                        ca.skill_path(bad)
                    self.assertIn(bad, str(cm.exception))
        finally:
            os.chmod(q, 0o644)


class TestWorkDirArg(unittest.TestCase):
    def test_两个调用方的拒绝各说各的_不许混成一句(self):
        # stderr 那一行是 agent 唯一的线索。混成一句「要进 status 的数据行和
        # 兜底句的白名单行」就是对两个调用方**各说了一半假话**：--dir 不进白名单，
        # --skill 不进 status 的列（skills 刻意不进列，见 new_meta）。
        # 本仓刚在 ensure_isolation 上吃过「错理由比没理由更危险」的亏。
        with self.assertRaises(argparse.ArgumentTypeError) as d:
            ca.work_dir("/tmp/a\tb")
        with self.assertRaises(argparse.ArgumentTypeError) as k:
            ca.skill_path("/abs/a\tb/SKILL.md")
        self.assertIn("status", str(d.exception))
        self.assertNotIn("白名单", str(d.exception))
        self.assertIn("白名单", str(k.exception))
        self.assertNotIn("status", str(k.exception))

    def test_收一个正常目录路径(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.assertEqual(ca.work_dir(str(d)), str(d))

    def test_含制表符或换行的目录被拒_而mkdir和is_dir全都放行(self):
        # 前提断言就是这条测试存在的理由：文件系统那一层**根本不管**，
        # 2026-09-19 实测含 \t 和 \n 的目录 mkdir / resolve() / is_dir() 全过。
        weird = pathlib.Path(tempfile.mkdtemp()) / "a\tb\nc"
        weird.mkdir()
        self.assertTrue(weird.is_dir(), "前提不成立：带控制字符的目录建不起来，这条就没意义了")
        self.assertIn("\n", str(weird.resolve()), "前提不成立：resolve 居然把换行吃掉了")
        for bad in (str(weird), "/tmp/a\tb", "/tmp/a\nb"):
            with self.subTest(bad=bad):
                with self.assertRaises(argparse.ArgumentTypeError) as cm:
                    ca.work_dir(bad)
                self.assertIn("控制字符", str(cm.exception))


class TestParser(_HomeSandbox):
    def test_run的五个参数一个都不能少(self):
        parser = ca.build_parser()
        for missing in ["--task", "--dir", "--brief", "--effort", "--account"]:
            # --no-skill 不加的话这条就静默退化成同义反复：缺 --skill/--no-skill
            # 照样 SystemExit，于是不管 --task/--dir/… 还必不必填，它都绿。
            argv = ["run", "--task", "t", "--dir", "/tmp", "--brief", "b.md",
                    "--effort", "low", "--account", "default", "--no-skill"]
            i = argv.index(missing)
            del argv[i:i + 2]
            with self.subTest(missing=missing), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_effort五个档位都收(self):
        parser = ca.build_parser()
        for e in ca.EFFORTS:
            with self.subTest(effort=e):
                args = parser.parse_args(["run", "--task", "t", "--dir", "/tmp",
                                          "--brief", "b.md", "--effort", e,
                                          "--account", "default", "--no-skill"])
                self.assertEqual(args.effort, e)

    def test_effort只收这五个档位(self):
        parser = ca.build_parser()
        # codex 对 `-c model_reasoning_effort=bogus` 静默接受、banner 照打，
        # argparse 的 choices 是唯一守门员
        for bad in ("中等", "ultra", "minimal"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parser.parse_args(["run", "--task", "t", "--dir", "/tmp", "--brief", "b.md",
                                   "--effort", bad, "--account", "default", "--no-skill"])

    def test_任务名校验挂在五个子命令上_结构上绕不过(self):
        parser = ca.build_parser()
        for argv in (["run", "--task", "a|b", "--dir", "/tmp", "--brief", "b.md",
                      "--effort", "low", "--account", "default", "--no-skill"],
                     ["status", "a|b"],
                     ["resume", "a|b", "--brief", "b.md", "--effort", "low", "--no-skill"],
                     ["stop", "a|b"],
                     ["interrupt-and-resume", "a|b", "--brief", "b.md", "--effort", "low",
                      "--no-skill"]):
            with self.subTest(cmd=argv[0]), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_resume和stop不收account_账号是查出来的(self):
        parser = ca.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["resume", "t", "--brief", "b.md", "--effort", "low",
                               "--no-skill", "--account", "default"])

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
        | `--json` | 同一份事实两种呈现，要永远保持一致——正是本仓一路在消灭的东西。列表形态已经机器切得开（见 status_row），单任务查询用退出码就够 |

        前两个是「会造成误用」，中间四个是「由工具派生」，`--json` 是「第二种呈现」。
        （`--json` 没有对应的 assertRaises：它和别的不一样，不是「给了会出事」，
        而是「根本不该存在」——真加了它，红的会是 status_row 那一整组契约测试。）
        """
        parser = ca.build_parser()
        for bad in ["--timeout", "--background", "-o", "--log", "--model", "--sandbox"]:
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parser.parse_args(["run", "--task", "t", "--dir", "/tmp", "--brief", "b.md",
                                   "--effort", "low", "--account", "default", "--no-skill",
                                   bad, "x"])


class TestInterruptAndResumeParser(_HomeSandbox):
    """参数规则与 resume **逐条一致**：任务名走 type=task_name、--brief 只收文件
    路径、--effort 必填无默认、不收 --account。同一个工具里 prompt 只有一种传法。
    """

    def test_与resume同一张参数表_少一个都不收(self):
        parser = ca.build_parser()
        for missing in ["--brief", "--effort"]:
            argv = ["interrupt-and-resume", "t", "--brief", "b.md", "--effort", "low",
                    "--no-skill"]
            i = argv.index(missing)
            del argv[i:i + 2]
            with self.subTest(missing=missing), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_effort五档正反都验(self):
        parser = ca.build_parser()
        for e in ca.EFFORTS:
            with self.subTest(effort=e):
                args = parser.parse_args(["interrupt-and-resume", "t", "--brief", "b.md",
                                          "--effort", e, "--no-skill"])
                self.assertEqual(args.effort, e)
        for bad in ("中等", "ultra", "minimal"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parser.parse_args(["interrupt-and-resume", "t", "--brief", "b.md",
                                   "--effort", bad, "--no-skill"])

    def test_不收account_账号是查出来的(self):
        with self.assertRaises(SystemExit):
            ca.build_parser().parse_args(["interrupt-and-resume", "t", "--brief", "b.md",
                                          "--effort", "low", "--no-skill",
                                          "--account", "default"])

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
                                   "--effort", "low", "--no-skill", bad, "x"])

    def test_子命令接到的确实是这条命令的实现(self):
        # TestInterruptAndResumeOrder 是直接拿 Namespace 调命令体的，接错了函数它测不出来。
        # 这里是命令行到实现之间唯一那根线。
        args = ca.build_parser().parse_args(
            ["interrupt-and-resume", "t", "--brief", "b.md", "--effort", "low", "--no-skill"])
        self.assertIs(args.func, ca.cmd_interrupt_and_resume)
        self.assertEqual((args.task, args.brief, args.effort), ("t", "b.md", "low"))


class TestSkillWhitelistFlags(_HomeSandbox):
    """白名单是**每一轮**的事，不是任务的事——所以三条带 prompt 的命令各收一份。

    二选一**必填**而不是缺省成空白名单：这个工具要交给其他 agent 用，省略时
    分不清「调用方决定不给」和「调用方根本不知道有这个参数」。强制显式把
    「没想过」变成 exit 2 当场报错，而这个拒绝是即时且完全可恢复的
    （加个参数重跑，零损失），不像 --effort/--account 写错要花钱才发现。
    """

    PROMPT_CMDS = ("run", "resume", "interrupt-and-resume")

    def setUp(self):
        super().setUp()
        self.skill_a = self.home / "a_SKILL.md"
        self.skill_b = self.home / "b_SKILL.md"
        for q in (self.skill_a, self.skill_b):
            q.write_text("---\nname: x\n---\n")

    def _argv(self, cmd, *tail):
        base = {"run": ["run", "--task", "t", "--dir", "/tmp", "--brief", "b.md",
                        "--effort", "low", "--account", "default"],
                "resume": ["resume", "t", "--brief", "b.md", "--effort", "low"],
                "interrupt-and-resume": ["interrupt-and-resume", "t", "--brief", "b.md",
                                         "--effort", "low"]}[cmd]
        return base + list(tail)

    def test_三条带prompt的命令都必须二选一_都不给就拒(self):
        parser = ca.build_parser()
        for cmd in self.PROMPT_CMDS:
            with self.subTest(cmd=cmd), self.assertRaises(SystemExit):
                parser.parse_args(self._argv(cmd))

    def test_三条命令都不许同时给(self):
        # 正面控制是**必须的**：单给任一个都过得去，下面那个拒绝才确实来自互斥。
        # 少了它，在参数还不存在的版本上这条也绿（两个都 unrecognized），
        # 是条永远不会红的空测试。
        parser = ca.build_parser()
        for cmd in self.PROMPT_CMDS:
            with self.subTest(cmd=cmd):
                self.assertEqual(parser.parse_args(self._argv(cmd, "--no-skill")).skills, ())
                with self.assertRaises(SystemExit):
                    parser.parse_args(self._argv(cmd, "--skill", str(self.skill_a), "--no-skill"))

    def test_no_skill解析成空白名单(self):
        parser = ca.build_parser()
        for cmd in self.PROMPT_CMDS:
            with self.subTest(cmd=cmd):
                self.assertEqual(parser.parse_args(self._argv(cmd, "--no-skill")).skills, ())

    def test_两支都给不可变序列_args_skills只有一种类型(self):
        """`args.skills` 的类型只能有**一种**，而且哪一支都改不动。

        argparse 现成的两件零件都有毛病：`const=` 在同一个 parser 上是**同一个
        对象**（给 `[]` 的话两次 parse 共享一个 list，谁原地改一下就污染另一次），
        `action="append"` 给的又是 list。两支类型不同，调用方写
        `args.skills == []` 会在一支上踩空——而那是最可能的下一个误用。
        统一成 tuple：两条软约定（别原地改、别拿 `== []` 比）一起消失。
        """
        parser = ca.build_parser()
        empty = parser.parse_args(self._argv("run", "--no-skill")).skills
        one = parser.parse_args(self._argv("run", "--skill", str(self.skill_a))).skills
        self.assertEqual(empty, ())
        self.assertEqual(one, (str(self.skill_a),))
        for got in (empty, one):
            with self.subTest(got=got), self.assertRaises(AttributeError):
                got.append("/x/SKILL.md")
        # 跨 parse 不许渗：拼在一个共享对象上的话，第二次会带上第一次那条
        self.assertEqual(
            parser.parse_args(self._argv("resume", "--skill", str(self.skill_b))).skills,
            (str(self.skill_b),), "上一次 parse 的 --skill 渗过来了")

    def test_skill可重复且保持给定顺序(self):
        parser = ca.build_parser()
        for cmd in self.PROMPT_CMDS:
            with self.subTest(cmd=cmd):
                args = parser.parse_args(self._argv(
                    cmd, "--skill", str(self.skill_a), "--skill", str(self.skill_b)))
                self.assertEqual(args.skills, (str(self.skill_a), str(self.skill_b)))

    def test_status和stop不收这两个参数_它们不带prompt(self):
        # 这条**前后都绿**，它守的是「别顺手给 status 也加上」：白名单是发 prompt
        # 那一刻的事，status/stop 根本不发 prompt，多一个参数就多一个误用机会。
        # 正面控制钉住「不带这两个参数时它们是收的」，否则整条是空的。
        parser = ca.build_parser()
        for cmd in ("status", "stop"):
            with self.subTest(cmd=cmd):
                self.assertEqual(parser.parse_args([cmd, "t"]).task, "t")
                for flag in ("--no-skill", "--skill"):
                    with self.subTest(flag=flag), self.assertRaises(SystemExit):
                        parser.parse_args([cmd, "t", flag, str(self.skill_a)])

    def test_dir的控制字符校验真的挂在命令行上(self):
        # 纯函数写对、parser 没挂 type= 的话，bug 原样还在——这是唯一那根线。
        # 正面控制是**必须的**：只钉「坏值被拒」的话，在 --no-skill 还不存在的
        # 版本上这条也绿（整条命令本来就被拒），永远不会红。
        parser = ca.build_parser()
        good = self._argv("run", "--no-skill")
        self.assertEqual(parser.parse_args(good).dir, "/tmp", "前提不成立：好的那条都过不去")
        bad = list(good)
        bad[bad.index("--dir") + 1] = "/tmp/a\tb"
        with self.assertRaises(SystemExit):
            parser.parse_args(bad)

    def test_skill的路径校验真的挂在命令行上(self):
        parser = ca.build_parser()
        for cmd in self.PROMPT_CMDS:
            with self.subTest(cmd=cmd):
                good = self._argv(cmd, "--skill", str(self.skill_a))
                self.assertEqual(parser.parse_args(good).skills, (str(self.skill_a),),
                                 "前提不成立：好的那条都过不去，坏的被拒就说明不了任何事")
                with self.assertRaises(SystemExit):
                    parser.parse_args(self._argv(cmd, "--skill", "relative/SKILL.md"))


class TestRunGuards(_HomeSandbox):
    def setUp(self):
        super().setUp()
        self.workdir = self.home / "repo"
        self.workdir.mkdir()
        self.brief = self.home / "brief.md"
        self.brief.write_text("干活")

    def _args(self, **over):
        argv = ["run", "--task", over.get("task", "t"), "--dir", over.get("dir", str(self.workdir)),
                "--brief", over.get("brief", str(self.brief)), "--effort", "low",
                "--account", "default", "--no-skill"]
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
             "--effort", "low", "--account", "default", "--no-skill"])
        def grab(*a, **k):
            seen["argv"] = a[0]
            return mock.DEFAULT       # 别写成 `x or mock.DEFAULT`：x 是真值时就把它返回去了

        with _no_codex() as popen:
            popen.side_effect = grab
            ca.cmd_run(args)
        cd = seen["argv"][seen["argv"].index("--cd") + 1]
        self.assertTrue(pathlib.Path(cd).is_absolute(), f"--cd 拿到的是 {cd}")
        self.assertEqual(pathlib.Path(cd), self.workdir.resolve())

    def test_dir是软链且真身含控制字符时拒跑_入口那道守的是原始串(self):
        # work_dir 挂在 argparse 的 type= 上，它看到的是**命令行上那个串**；
        # 而落进元数据、随后进 status 数据行的是 `resolve()` 之后的真身。
        # 软链一跨，入口那道就绕过去了——所以 resolve 之后必须再守一次。
        ca.ensure_isolation("default")
        real = self.home / "a\tb\nc"
        real.mkdir()
        link = self.home / "link"
        link.symlink_to(real)
        self.assertTrue(link.is_dir(), "前提不成立：软链没指到目录上")
        self.assertEqual(ca.work_dir(str(link)), str(link),
                         "前提不成立：入口这道本来就该放行它，否则这条测的不是软链那个洞")
        self.assertIn("\n", str(link.resolve()), "前提不成立：resolve 居然没解出真身")
        # _no_codex 是**必须**的：这条闸没装上时 cmd_run 会一路走到 spawn，
        # 不挡住就真的把 codex 叫起来了（实测过，单测绝不能发网络请求）。
        with _no_codex(), self.assertRaises(ca.Rejected) as cm:
            ca.cmd_run(self._args(dir=str(link)))
        self.assertIn("控制字符", cm.exception.message)

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
            ["resume", task, "--brief", str(self.brief), "--effort", "low", "--no-skill"])

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
        """这一类测的是**命令体的执行顺序**，不是参数表——参数表归
        TestInterruptAndResumeParser 和 TestSkillWhitelistFlags，那里用真 parser
        从命令行一路验下来。所以这里直接搭 Namespace：四个字段逐个显式写出，
        不走默认值（skills 也照写，缺省成空就等于替调用方做了决定）。
        """
        return argparse.Namespace(
            task=task, brief=str(self.brief if brief is None else brief), effort="low",
            skills=())

    def test_在跑时顺序是先打断再续跑(self):
        order = []
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "interrupt_codex",
                               side_effect=lambda *a: order.append("打断")), \
             mock.patch.object(ca, "_resume_round",
                               side_effect=lambda *a: order.append("续跑") or 0):
            self.assertEqual(ca.cmd_interrupt_and_resume(self._args()), 0)
        self.assertEqual(order, ["打断", "续跑"], "顺序反了就会撞写锁")

    def test_没在跑时不发信号直接续跑(self):
        # 调用方无法可靠知道自己在哪种情况——查完到动手之间任务可能刚好跑完
        with mock.patch.object(ca, "find_codex_pid", return_value=None), \
             mock.patch.object(ca, "interrupt_codex") as ic, \
             mock.patch.object(ca, "_resume_round", return_value=0):
            self.assertEqual(ca.cmd_interrupt_and_resume(self._args()), 0)
        ic.assert_not_called()

    def test_没有session_id时绝不发信号_那一轮白毁且拿不回来(self):
        # 闸序一坏会怎样：实测「把 check_can_resume 挪到发信号之后」这个突变，
        # 在等待还挂在本命令上的那个版本里要 **180.3 秒**才红，而且报的是
        # 「收到 INT 后 60 秒还没退出」——指错方向。等待搬进 run_codex 之后，
        # 这几条闸测试一个 mock 都不需要：它们在 _resume_round 之前就拒绝了。
        ca.write_meta(self.d, "t2", _full_meta("t2", dir=str(self.workdir)))
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "interrupt_codex") as ic, \
             mock.patch.object(ca.os, "kill") as k:
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_interrupt_and_resume(self._args("t2"))
        self.assertIn("session id", cm.exception.message)
        ic.assert_not_called()
        k.assert_not_called()

    def test_brief自带兜底句时绝不发信号_这条纯检查必须前移(self):
        """spec 的判据精化：**凡是对「调用方已经交给我们的输入」的纯检查，
        一律在任何不可逆动作之前做完。**

        「brief 里已经有兜底句」完全由调用方交进来的那个文件决定，动手之前就
        问得出来。留在 prepend_skill_guard 里的话，这条路会先发 INT 把当前轮
        截断、再因为一个**本可提前发现**的理由拒绝——白烧一轮，即使可恢复
        也是浪费。所以它进了 check_can_resume，和另外三道闸并排。
        """
        ca.write_meta(self.d, "t9", _full_meta("t9", session_id="s9", dir=str(self.workdir)))
        bad = self.home / "自带兜底句.md"
        bad.write_text("**不得使用任何 skill。**\n\n顺便把 X 也改了")
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "interrupt_codex") as ic, \
             mock.patch.object(ca.os, "kill") as k:
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_interrupt_and_resume(self._args("t9", brief=bad))
        self.assertEqual(cm.exception.code, 2, "护栏拒绝走 2，不许和判据结论撞码")
        self.assertIn("--no-skill", cm.exception.message)
        ic.assert_not_called()
        k.assert_not_called()

    def test_工作目录没了时绝不发信号(self):
        ca.write_meta(self.d, "t3", _full_meta("t3", session_id="s3",
                                               dir=str(self.home / "已经删了的worktree")))
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "interrupt_codex") as ic, \
             mock.patch.object(ca.os, "kill") as k:
            with self.assertRaises(ca.Rejected) as cm:
                ca.cmd_interrupt_and_resume(self._args("t3"))
        self.assertIn("不在了", cm.exception.message)
        ic.assert_not_called()
        k.assert_not_called()

    def test_brief不是文件时绝不发信号(self):
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
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
             mock.patch.object(ca, "_resume_round", return_value=0):
            self.assertEqual(ca.cmd_interrupt_and_resume(self._args()), 0)
        ic.assert_not_called()
        # 「零调用」要钉 os.kill 本身，不能只钉 interrupt_codex：在那一支里加一行
        # 裸 os.kill 也会红，但红的原因是 ProcessLookupError（4242 不存在）这个
        # **巧合**——pid 若恰好存在就是绿的。同一个类里四条闸测试都钉了 os.kill。
        k.assert_not_called()

    def test_上一轮的打断痕迹不算数_本轮还是要发INT(self):
        # 反面钉一道：判据必须只看**本轮**。看全文的话，一个被打断过的任务
        # 之后永远发不出 INT 了。
        log = ca._log_path(self.d, "t")
        log.write_text(ca.round_separator("run", "t", "2026-09-19T00:00:00") + "\n"
                       + ca.INTERRUPT_MARK + "\n"
                       + ca.round_separator("resume", "t", "2026-09-19T00:00:01") + "\n干净\n")
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca, "interrupt_codex") as ic, \
             mock.patch.object(ca, "_resume_round", return_value=0):
            ca.cmd_interrupt_and_resume(self._args())
        # **打给谁**也要钉：传成报告路径的话，痕迹写进**报告** → judge 看到非空
        # 报告、无错误行 → 判 success、退出码 0。不是崩，是静默说谎。
        # 实跑确认：只钉 assert_called_once() 的话这个突变 156 条全绿。
        ic.assert_called_once_with(4242, ca._log_path(self.d, "t"), "interrupt-and-resume")

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
    """兜底句是 SKILL.md 印给调用方的**对外承诺**，三条路都必须真的加上。

    `prepend_skill_guard` 自己有纯函数单测，但那只证明「这个函数会加」，
    不证明「run / resume / interrupt-and-resume 真的调了它、而且传的是**本轮**
    的白名单」。把调用点换成裸 `read_text()` 的突变曾经**全部存活**——
    一条印出去的承诺，没有任何东西守着。

    结构性防线是 CODEX_HOME 隔离（codex 结构上看不见用户的 skill），这句是
    内容层的第二道；白名单则是这一道上唯一的开口，所以它长什么样必须钉在
    **真正送进 codex argv 的那段文本**上，不是钉在派生函数的返回值上。
    """

    def setUp(self):
        super().setUp()
        self.workdir = self.home / "repo"
        self.workdir.mkdir()
        self.brief = self.home / "brief.md"
        self.brief.write_text("干活")
        self.skill = self.home / "tdd_SKILL.md"
        self.skill.write_text("---\nname: tdd\n---\n")

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

    def _meta_for_resume(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t", session_id="s1", dir=str(self.workdir)))
        return d

    def test_run这条路_无白名单(self):
        ca.ensure_isolation("default")
        args = ca.build_parser().parse_args(
            ["run", "--task", "t", "--dir", str(self.workdir), "--brief", str(self.brief),
             "--effort", "low", "--account", "default", "--no-skill"])
        brief = self._brief_codex_actually_got(lambda: ca.cmd_run(args))
        self.assertTrue(brief.startswith("**不得使用任何 skill。**"),
                        f"codex 实际收到的是：{brief[:60]!r}")
        self.assertIn("干活", brief)

    def test_run这条路_有白名单时路径真的进了argv(self):
        ca.ensure_isolation("default")
        args = ca.build_parser().parse_args(
            ["run", "--task", "t", "--dir", str(self.workdir), "--brief", str(self.brief),
             "--effort", "low", "--account", "default", "--skill", str(self.skill)])
        brief = self._brief_codex_actually_got(lambda: ca.cmd_run(args))
        self.assertTrue(brief.startswith("**不得使用任何 skill，以下几个除外（动手前先逐个读一遍）：**"),
                        f"codex 实际收到的是：{brief[:60]!r}")
        self.assertIn(f"- {self.skill}", brief)
        # 钉在**真正送进 argv 的那段文本**上：许可送到了不等于指令送到了，
        # 而没有指令就没有那行 `cat`，judge 看不见「它压根没读」
        self.assertIn("动手前先逐个读一遍", brief)
        self.assertIn("干活", brief)

    def test_resume这条路_有白名单(self):
        self._meta_for_resume()
        args = ca.build_parser().parse_args(
            ["resume", "t", "--brief", str(self.brief), "--effort", "low",
             "--skill", str(self.skill)])
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            brief = self._brief_codex_actually_got(lambda: ca.cmd_resume(args))
        self.assertTrue(brief.startswith("**不得使用任何 skill，以下几个除外（动手前先逐个读一遍）：**"),
                        f"codex 实际收到的是：{brief[:60]!r}")
        self.assertIn(f"- {self.skill}", brief)
        self.assertIn("动手前先逐个读一遍", brief)

    def test_interrupt_and_resume这条路_无白名单(self):
        self._meta_for_resume()
        args = ca.build_parser().parse_args(
            ["interrupt-and-resume", "t", "--brief", str(self.brief), "--effort", "low",
             "--no-skill"])
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            brief = self._brief_codex_actually_got(
                lambda: ca.cmd_interrupt_and_resume(args))
        self.assertTrue(brief.startswith("**不得使用任何 skill。**"),
                        f"codex 实际收到的是：{brief[:60]!r}")
        self.assertIn("干活", brief)


class TestStatusIsSplittable(_HomeSandbox):
    """列表形态的 status 必须**机器切得开**。这是本改动的全部理由。

    旧格式是 `f"{task:<24} {account:<8} {state:<8} {reason}  {dir}"`，而 reason
    含空格 → 后面任何一列都取不出来。**7 个 Verdict 构造点里有 4 个的 reason
    真的含空格**：「本轮被 INT 打断，上下文保留——接着 resume 即可，不用重跑」、
    「报告在，但本轮日志有 N 条未分类的 codex 错误」、「会话被写锁占住（……曾被
    SIGTERM 杀过），只能新起一个任务」、「pid=N 存活」。
    （spec 和计划都写成「5 个」，实跑逐条数过是 4 个——它们把 suspect 那条重复
    数了一次。另外 spec §1 举的那个例子「报告缺失或为空＝没正常收尾」**不含
    空格**，照它写的测试测不到任何东西，所以这里一律用真的含空格的那几条。）

    真正缺的只有一样：列表形态下每行的 state（单任务查询用退出码就够了，
    dir/account/effort/session_id 早就在 tasks/<task>.json 里）。

    采纳的方案是**换列序、不换格式**。初稿的制表符方案被实测否掉：按 tabstop=8
    量四行真实 status 输出的各列屏幕起始列，[0,16,24,32,40,80] /
    [0,16,24,32,40,88] / [0,8,16,24,32,40] / [0,32,40,56,64,80]——四行没有一列
    对齐，而空格定宽是稳定的 [0,25,34,43]。
    """

    # 真的含空格的那一条（judge 的 interrupted 分支原文），不是造出来的例子
    REASON_WITH_SPACES = "本轮被 INT 打断，上下文保留——接着 resume 即可，不用重跑"

    def test_前四列用split切得开_reason含空格也不影响(self):
        row = ca.status_row(_full_meta("t1", dir="/abs/repo"),
                            ca.Verdict("failed", self.REASON_WITH_SPACES, []))
        self.assertGreater(len(self.REASON_WITH_SPACES.split()), 1,
                           "前提不成立：reason 不含空格的话，这条根本测不到东西")
        self.assertEqual(row.split(maxsplit=4)[:4], ["t1", "default", "failed", "1"])

    def test_第五段是工作目录加reason_dir含空格也切得开(self):
        row = ca.status_row(_full_meta("t1", dir="/abs/my repo"),
                            ca.Verdict("failed", self.REASON_WITH_SPACES, []))
        tail = row.split(maxsplit=4)[4]
        self.assertTrue(tail.startswith("/abs/my repo"), tail)
        self.assertTrue(tail.endswith(self.REASON_WITH_SPACES), tail)

    def test_退出码那一列的绝对值_五态逐个(self):
        # 绝对值，不写 ca.EXIT[state]：那样两边一起动，EXIT["running"]=0 这种
        # 突变照样绿——而那正是 status && deploy 提前部署的那个 bug。
        for state, code in (("success", "0"), ("failed", "1"), ("suspect", "3"),
                            ("running", "4"), ("interrupted", "130")):
            with self.subTest(state=state):
                row = ca.status_row(_full_meta("t1", dir="/abs/repo"),
                                    ca.Verdict(state, "一句人话", []))
                self.assertEqual(row.split(maxsplit=4)[3], code)

    def test_明细行有缩进_数据行没有(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t"))
        ca._log_path(d, "t").write_text(ca.round_separator("run", "t", "2026-09-19T00:00:00")
                                        + "\n" + ERR_FATAL + "\n")
        ca._report_path(d, "t").write_text("干完了\n")
        screen = io.StringIO()
        with mock.patch.object(ca, "find_codex_pid", return_value=None), \
             contextlib.redirect_stdout(screen):
            ca.cmd_status(ca.build_parser().parse_args(["status"]))
        lines = [l for l in screen.getvalue().splitlines() if l]
        data = [l for l in lines if not l[0].isspace()]
        detail = [l for l in lines if l[0].isspace()]
        self.assertEqual(len(data), 1, f"数据行不止一行：{lines}")
        self.assertTrue(detail, "前提不成立：这一轮没有明细行，那这条测不到可分性")
        self.assertEqual(data[0].split(maxsplit=4)[:3], ["t", "default", "suspect"])

    def test_reason含制表符时当场拒绝(self):
        with self.assertRaises(ca.Rejected) as cm:
            ca.status_row(_full_meta("t1", dir="/abs/repo"),
                          ca.Verdict("failed", "坏\treason", []))
        self.assertEqual(cm.exception.code, 2)

    def test_工作目录含换行时当场拒绝(self):
        with self.assertRaises(ca.Rejected):
            ca.status_row(_full_meta("t1", dir="/abs/a\nb"),
                          ca.Verdict("failed", "一句人话", []))

    def test_账号含空白时当场拒绝_这一列的值来自元数据文件(self):
        # 前四列「天生无空格」这句话对账号**不成立**。目录名那一侧由
        # account_choices() 在入口拒（见 TestIsolation），而 status 第二列读的是
        # tasks/<task>.json 里的 account 字段——_load_meta 只校验**键**在不在，
        # 值长什么样一概不管，所以这一道是独立的第二个入口，不是重复防御。
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t", account="bad acct"))
        self.assertEqual(ca._load_meta(ca.meta_path(d, "t"))["account"], "bad acct",
                         "前提不成立：元数据这一侧居然校验了 account 的值，那就不是真的洞")
        with self.assertRaises(ca.Rejected):
            ca.status_row(_full_meta("t1", account="bad acct", dir="/abs/repo"),
                          ca.Verdict("failed", "一句人话", []))

    def test_任务名含空白也拒绝_而reason含空格照样放行(self):
        # 正面控制在这里是承重的：reason 含空格**必须**放行（它在第五段，
        # 那正是换列序买到的东西）。少了它，一条「什么都拒」的假实现也全绿。
        self.assertIn(" ", ca.status_row(_full_meta("t1", dir="/abs/repo"),
                                         ca.Verdict("success", "有 空 格 的 reason", [])))
        with self.assertRaises(ca.Rejected):
            ca.status_row(_full_meta("t 1", dir="/abs/repo"),
                          ca.Verdict("failed", "一句人话", []))
        # 状态那一列自己带空白的话，它压根就不是五态之一——归 _require_enum 管，
        # 那是内部调用方传错枚举，当场 ValueError，不是护栏拒绝
        with self.assertRaises(ValueError):
            ca.status_row(_full_meta("t1", dir="/abs/repo"),
                          ca.Verdict("fai led", "一句人话", []))

    def test_不是五态之一当场炸_不给一个裸KeyError(self):
        # 退出码那一列是 EXIT[state]，state 写错的话裸下标只给一个 KeyError，
        # 调用方看不出是什么坏了。和 kind/cause 一样走 _require_enum。
        with self.assertRaises(ValueError) as cm:
            ca.status_row(_full_meta("t1", dir="/abs/repo"),
                          ca.Verdict("done", "一句人话", []))
        self.assertIn("state", str(cm.exception))

    def test_真实status输出每行一个任务_两个任务各切出四列(self):
        # 刻意选 interrupted 这一支：它的 reason **真的含空格**，端到端走一遍才算
        # 把「reason 排在最后」这件事钉住。选 failed 那一支测不到——它的 reason
        # （「报告缺失或为空＝没正常收尾」）一个空格都没有。
        d = ca.ensure_isolation("default")
        for name in ("alpha", "beta"):
            ca.write_meta(d, name, _full_meta(name, dir="/abs/repo"))
            ca._log_path(d, name).write_text(
                ca.round_separator("run", name, "2026-09-19T00:00:00") + "\n"
                + ca.INTERRUPT_MARK + "\n")
        screen = io.StringIO()
        with mock.patch.object(ca, "find_codex_pid", return_value=None), \
             contextlib.redirect_stdout(screen):
            ca.cmd_status(ca.build_parser().parse_args(["status"]))
        data = [l for l in screen.getvalue().splitlines() if l and not l[0].isspace()]
        self.assertEqual([l.split(maxsplit=4)[0] for l in data], ["alpha", "beta"])
        for line in data:
            with self.subTest(line=line):
                self.assertEqual(line.split(maxsplit=4)[1:4], ["default", "interrupted", "130"])
                tail = line.split(maxsplit=4)[4]
                self.assertTrue(tail.startswith("/abs/repo"), tail)
                self.assertIn(" ", tail[len("/abs/repo"):].strip(),
                              "前提不成立：这一支的 reason 不含空格，那就没测到列序")


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
             "--effort", "low", "--account", "default", "--no-skill"])

    def _resume_args(self):
        return ca.build_parser().parse_args(
            ["resume", "t", "--brief", str(self.brief), "--effort", "low", "--no-skill"])

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
            ["interrupt-and-resume", "t", "--brief", str(self.brief), "--effort", "low",
             "--no-skill"])
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

    # 日志**刻意非空**，而且头部就是真实日志的头部（分隔符行 + session id 行）。
    # 在空日志上测留痕是模块 docstring 点名的第 1 类空测试：**前提本身不成立**
    # ——空文件上「从 0 覆盖写」和「O_APPEND 追加」结果一模一样，于是把
    # interrupt_codex 的 O_APPEND 去掉这个突变**全绿存活**。实际后果两条：
    #   痕迹落在 start_offset 之前 → read_round 看不到 → 判 failed
    #     （interrupted 这个分支存在的理由被静默重新引入）
    #   日志头部的分隔符和 session id 被覆盖 → resume 再也回不来
    # docstring 把「日志追加而非覆盖」列在「已全部被杀」里，那条只覆盖了
    # run_codex 的 "ab"，**没覆盖 interrupt_codex**。
    HEAD = (ROUND_SEP_SAMPLE + "\n"
            + "session id: 01a0b408-f718-7ff3-8123-d5202551acba\n")

    def setUp(self):
        self.log = pathlib.Path(tempfile.mkdtemp()) / "t.log"
        self.log.write_text(self.HEAD)

    def _assert_appended(self, 说明):
        """痕迹必须**追加在头部之后**，头部原样还在。"""
        text = self.log.read_text()
        self.assertTrue(text.startswith(self.HEAD), f"{说明}：日志头部被覆盖了")
        self.assertIn("session id:", text, f"{说明}：session id 没了，resume 再也回不来")
        self.assertGreater(text.index(ca.INTERRUPT_MARK), len(self.HEAD) - 1,
                           f"{说明}：痕迹落在了头部之前")

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
            ca.interrupt_codex(proc.pid, self.log, "stop")
            self.assertIn(ca.INTERRUPT_MARK, self.log.read_text())
            self._assert_appended("发信号留痕")
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
        ca.interrupt_codex(dead.pid, self.log, "stop")
        self.assertEqual(self.log.read_text(), self.HEAD, "没送出信号却动了日志")

    def test_痕迹带上来源_三条路读得出是哪一条(self):
        """三条路的含义完全不同，日志里必须分得开。

        `stop` 和 `interrupt-and-resume` 是有人**故意**停它；
        `外部信号转发` 在 run_in_background 下**根本不该发生**——它出现就等于
        前台误跑被 2 分钟超时杀掉了。不带来源的话日志里只剩一句「被打断了」，
        下一个人读不出「你当时用错了启动方式」。
        这是把一条编不进去的软约定被违反，变成日志里可读的诊断。
        """
        for cause in ("stop", "interrupt-and-resume", "外部信号转发"):
            with self.subTest(cause=cause):
                self.log.write_text(self.HEAD)
                with mock.patch.object(ca.os, "kill"):
                    ca.interrupt_codex(4242, self.log, cause)
                self.assertIn(f"[{cause}]", self.log.read_text())
                # 带了来源也仍然是合法痕迹：判据不受影响
                self.assertTrue(ca.has_interrupt_mark(self.log.read_text()),
                                f"带上 [{cause}] 之后判据认不出这是打断了")

    def test_短写也要把痕迹写完整_半截痕迹比没有痕迹更坏(self):
        """`os.write` 的返回值不能丢：它可能**短写**（磁盘满／配额耗尽）。

        `except OSError: pass` 那条降级只挡「一个字节都没写进去」，挡不住
        「写进去一半」。复核用 RLIMIT_FSIZE 逼出过真实短写：84 字节只写进 60，
        痕迹被截断 → has_interrupt_mark 认不出 → judge 从 interrupted(130)
        退回 failed(1)。
        而**半截痕迹比没有痕迹更坏**：它既骗不过 _MARK_LINE，又污染了本轮文本。
        """
        self.log.write_text(self.HEAD)
        真write = os.write
        每次只写几个字节 = []

        def 短写(fd, data):
            每次只写几个字节.append(len(data))
            return 真write(fd, data[:7])      # 每次只吞 7 字节

        with mock.patch.object(ca.os, "kill"), \
             mock.patch.object(ca.os, "write", side_effect=短写):
            ca.interrupt_codex(4242, self.log, "stop")
        self.assertGreater(len(每次只写几个字节), 1,
                           "前提不成立：一次就写完了，这条测不到短写")
        self.assertTrue(ca.has_interrupt_mark(self.log.read_text()),
                        "痕迹被短写截断了，判据认不出这是一轮被打断的运行")
        self._assert_appended("短写补齐之后")

    def test_写不进去时不许把信号路径挂死(self):
        """补写循环的代价是「`os.write` 返回 0 就转不出去」——而这是在**信号
        处理器**里跑的，挂死比留半截痕迹坏得多（codex 的 tee 循环再也收不了尾）。

        真磁盘写满时内核抛 OSError（EFBIG），不返回 0；但这个前提不该靠指望，
        所以结构上就不让它转下去。
        """
        self.log.write_text(self.HEAD)
        调用次数 = []

        def 永远写不进去(fd, data):
            调用次数.append(1)
            if len(调用次数) > 50:
                self.fail("os.write 返回 0 时转不出去——信号路径被挂死了")
            return 0

        with mock.patch.object(ca.os, "kill"), \
             mock.patch.object(ca.os, "write", side_effect=永远写不进去):
            ca.interrupt_codex(4242, self.log, "stop")   # 必须能返回

    def test_非法cause当场被挡住_而且信号还没发出去(self):
        """docstring 里写「cause ∈ {…}」挡不住任何东西，散文不是约束。

        校验必须排在 `os.kill` **之前**：INT 发出去收不回来，先打断再发现
        cause 写错，那一轮白毁。
        """
        for bad in ("a\nb", None, "", "随便写的"):
            with self.subTest(bad=bad):
                self.log.write_text(self.HEAD)
                with mock.patch.object(ca.os, "kill") as k:
                    with self.assertRaises(ValueError):
                        ca.interrupt_codex(4242, self.log, bad)
                k.assert_not_called()
                self.assertEqual(self.log.read_text(), self.HEAD, "日志被动过了")

    def test_三个合法cause都收(self):
        for good in ca._CAUSES:
            with self.subTest(good=good):
                self.log.write_text(self.HEAD)
                with mock.patch.object(ca.os, "kill"):
                    ca.interrupt_codex(4242, self.log, good)
                self.assertTrue(ca.has_interrupt_mark(self.log.read_text()))

    def test_非法cause若被放过会怎样_这是上面那条为什么承重(self):
        """不测生产代码，钉的是**因果**。

        cause 里带换行的话，痕迹被劈成两行，`_MARK_LINE` 整行匹配当场认不出，
        judge 从 interrupted(130) 退回 failed(1)——这个分支存在的理由当场复活。
        """
        劈开的 = "\n" + ca.INTERRUPT_MARK + " [a\nb]\n"
        self.assertIn(ca.INTERRUPT_MARK, 劈开的, "前提不成立：样本里没有痕迹前缀")
        self.assertFalse(ca.has_interrupt_mark(劈开的),
                         "前提变了：痕迹被劈开之后居然还认得出，那上面那条校验就不承重了")

    def test_只发INT绝不发TERM(self):
        # SIGTERM 会让 thread 永久锁死，之后 resume 永远报 thread-store conflict，
        # 等多久都不释放，上下文全丢
        with mock.patch.object(ca.os, "kill") as k:
            ca.interrupt_codex(4242, self.log, "stop")
        k.assert_called_once_with(4242, signal.SIGINT)
        self._assert_appended("只发 INT 这条路")

    def test_刻意不给返回值(self):
        """「它本来就没在跑」由调用方在**调用之前**用 find_codex_pid 判，
        那才是判它的地方。给个 bool 出来，就多出一个「谁检查」的滥用面。
        """
        with mock.patch.object(ca.os, "kill"):
            self.assertIsNone(ca.interrupt_codex(4242, self.log, "stop"))


class TestWaitPreviousRoundEnds(_HomeSandbox):
    """「上一轮结束了没有」＝**两个都停了**，缺一不可。

        还有人写日志吗   _previous_writer_alive   包装器，身份记在元数据里
        会话还被占着吗   find_codex_pid           现场反查

    只看 codex 会在它**变僵尸那一刻**（实测 0.027s）就放行——僵尸的 cmdline 为空，
    argv 比对天然拒绝它——而那时日志还要再长好几秒。只看包装器会撞上**孤儿 codex**：
    SIGKILL 掉包装器之后 codex 存活并跑完（实测），续跑就撞上它的写锁。
    """

    def setUp(self):
        super().setUp()
        self.d = ca.ensure_isolation("default")

    def _写盘(self, **over):
        ca.write_meta(self.d, "t", _full_meta("t", **over))

    def test_没有上一轮就直接放行(self):
        self.assertFalse(ca.meta_path(self.d, "t").exists(), "前提不成立：元数据居然已经在了")
        self.assertTrue(ca._wait_previous_round_ends(self.d, "t", 5, 0.01))

    def test_没有元数据但codex还在跑_不许放行(self):
        """「文件不在就直接放行」是**多余且开洞**的。

        多余：`_previous_writer_alive` 自己第一件事就是查文件在不在。
        开洞：那条早返回**连 codex 那一半都跳过了**——元数据被手删（或换账号
        重跑把它挪走）而上一轮的 codex 还占着会话时，当场放行，新一轮直接撞上
        它的写锁。两个条件一个都不能少，没有「文件不在」这条例外。
        """
        self.assertFalse(ca.meta_path(self.d, "t").exists(), "前提不成立：元数据居然已经在了")
        with mock.patch.object(ca, "find_codex_pid", return_value=4242):
            self.assertFalse(ca._wait_previous_round_ends(self.d, "t", 0.3, 0.01))

    def test_两个都停了才放行(self):
        self._写盘()
        self.assertFalse(ca._previous_writer_alive(self.d, "t"), "前提不成立：默认身份居然还在写")
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            self.assertTrue(ca._wait_previous_round_ends(self.d, "t", 5, 0.01))

    def test_上一轮的writer还在写就等到超时(self):
        # **突变锁：只等 codex。** 只看 codex 的话这里会立刻放行——那正是病根本身。
        self._写盘(**_live_writer(self))
        self.assertTrue(ca._previous_writer_alive(self.d, "t"), "前提不成立：陪练居然不算在写")
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            self.assertFalse(ca._wait_previous_round_ends(self.d, "t", 0.3, 0.01))

    def test_孤儿codex_writer没了codex还在_不许放行(self):
        # **突变锁：只等 writer。** SIGKILL 掉包装器之后 codex 会存活并跑完（实测），
        # 只看包装器就会放行，续跑撞上它的写锁。
        self._写盘()
        self.assertFalse(ca._previous_writer_alive(self.d, "t"), "前提不成立")
        with mock.patch.object(ca, "find_codex_pid", return_value=4242):
            self.assertFalse(ca._wait_previous_round_ends(self.d, "t", 0.3, 0.01))

    def test_codex早变僵尸而writer还在排干时必须继续等(self):
        """**本次回归锁**，整条链一起验：真起一个 comm=codex、argv 对得上的陪练，
        杀掉但不回收 → 它是僵尸、`cmdline` 已空 → `find_codex_pid` 判 None，
        而上一轮的 writer 还在写 → 必须继续等到超时。
        """
        (self.d / "reports").mkdir(exist_ok=True)
        fake = _fake_codex(self.d)
        report = ca._report_path(self.d, "t")
        report.write_text("")
        # 陪练身份要在起僵尸**之前**拿：_live_writer 会建 Popen，而 Popen.__init__
        # 顺手调 subprocess._cleanup() 把僵尸回收掉（见 _wait_zombie）。
        写者 = _live_writer(self)
        proc = subprocess.Popen([str(fake), "-f", str(report)], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_argv(self, proc, str(report))
            self.assertEqual(ca.find_codex_pid(report), proc.pid, "前提不成立：活着时就找不到它")
            proc.kill()
            _wait_zombie(self, proc.pid)
            self.assertEqual(pathlib.Path(f"/proc/{proc.pid}/cmdline").read_bytes(), b"",
                             "前提不成立：僵尸的 cmdline 不空，那 argv 过滤就不是免费做对的")
            self.assertIsNone(ca.find_codex_pid(report), "前提不成立：僵尸居然还被当成在跑")
            self._写盘(**写者)
            self.assertTrue(ca._previous_writer_alive(self.d, "t"), "前提不成立：陪练居然不算在写")
            self.assertFalse(ca._wait_previous_round_ends(self.d, "t", 0.3, 0.01))
        finally:
            proc.kill()
            proc.wait()

    def test_等待期间绝不发任何信号(self):
        # 超时的正确处置是告诉调用方稍后再来，不是加大火力。
        # 升级到 SIGTERM 会让会话永久锁死，而那一步不可逆。
        self._写盘()
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca.os, "kill") as k:
            ca._wait_previous_round_ends(self.d, "t", 0.05, 0.01)
        k.assert_not_called()

    def test_等到了就立刻走_不把超时睡满(self):
        self._写盘()
        started = time.monotonic()
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            self.assertTrue(ca._wait_previous_round_ends(self.d, "t", 30, 0.01))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_真要等的时候先说一句_否则run变成静默挂起(self):
        """等待排在**分隔符之前**，所以这段时间里日志是死的，屏幕也不能死。

        不打印的话 `run` 会多出一个全新的静默挂起：用户看不出它在等谁、等多久。
        这句**只进屏幕不进日志**——日志里此刻还没有本轮的边界，写进去就落在
        上一轮里，把上一轮的判据弄脏。它当场就读得到，靠的是 `main()` 那行
        `sys.stdout.reconfigure(line_buffering=True)`。
        """
        self._写盘(**_live_writer(self))
        buf = io.StringIO()
        with mock.patch.object(ca, "find_codex_pid", return_value=None), \
             contextlib.redirect_stdout(buf):
            ca._wait_previous_round_ends(self.d, "t", 0.15, 0.01)
        self.assertIn("上一轮还在收尾", buf.getvalue())

    def test_不用等的时候一个字都不说(self):
        # 反面钉一道：无条件打印的话，那句话在 99% 的情况下是假的
        # （上一轮早就结束了），而假话说多了人就不看了。
        self._写盘()
        buf = io.StringIO()
        with mock.patch.object(ca, "find_codex_pid", return_value=None), \
             contextlib.redirect_stdout(buf):
            ca._wait_previous_round_ends(self.d, "t", 5, 0.01)
        self.assertEqual(buf.getvalue(), "")

    def test_两个常量的绝对值(self):
        """常量必须是**模块级**的，不是函数默认参数：测试要把它压到 0.3 秒，
        而仓库规范本来就不许用默认缺省值。

        **刻意只钉绝对值，一条「倍数余量」都不钉。** 排干多久由 codex 起的后代
        决定，**无上界**（起个后台服务就永不结束），所以 60 秒是「本工具愿意等
        多久」的**策略值**。原先那两条（`>= 1.854 * 30`、`< 0.964`）都把 codex 的
        INT 退出实测当成了上界，已删。
        """
        self.assertEqual(ca.ROUND_END_TIMEOUT, 60)
        self.assertEqual(ca.ROUND_END_POLL_INTERVAL, 0.2)


class TestRunCodexWaitsForPreviousRound(_HomeSandbox):
    """等待折进唯一的 spawn 入口——三条路都经过它，「哪个入口漏了」结构上不存在。"""

    def setUp(self):
        super().setUp()
        self.d = ca.ensure_isolation("default")

    def test_等的是磁盘上那份_不是传进来的meta(self):
        """**本版头号回归锁。**

        按参数读的话，`cmd_run` 传的是刚造好的 `new_meta()`——那一份的 writer 就是
        本进程，于是这一轮会去等**参数里记的那个进程**，而不是上一轮真正的写者。

        构造：磁盘那份记的是一个早没了的进程（→ 正确实现一路走完），**参数**那份
        记的是一个**还活着的别的进程**（→ 读错了就会等满 0.3 秒然后 reject）。
        判据同时钉**走通**和**耗时**，超时只给 0.3 秒，所以这条测试自己绝不挂死。

        **参数那份刻意不用本进程的身份**（计划原稿是那样写的）：
        `_previous_writer_alive` 对本进程恒答「停了」，拿本进程当参数的话，
        「改读参数」那个突变会被那条短路救活——实测确实存活。换成真陪练之后才杀得掉。
        """
        ca.write_meta(self.d, "t", _full_meta("t"))          # 磁盘：上一轮早停了
        活的 = _full_meta("t", **_live_writer(self))          # 参数：一个还活着的别的进程
        # 前提用 _read_stat_fields 表达，**不碰谓词本身**：突变要改的正是谓词的签名，
        # 用谓词写前提的话，突变会先把前提这一行炸成 TypeError——红是红了，
        # 但红的是「签名变了」，不是「等错了人」，指错方向（实跑踩过）。
        self.assertIsNone(ca._read_stat_fields(GONE_PID),
                          "前提不成立：磁盘那份记的进程居然还在")
        self.assertIsNotNone(ca._read_stat_fields(活的["writer_pid"]),
                             "前提不成立：参数那份记的进程不在，突变就不会卡住")
        started = time.monotonic()
        with mock.patch.object(ca, "ROUND_END_TIMEOUT", 0.3), \
             mock.patch.object(ca, "find_codex_pid", return_value=None), \
             _no_codex():
            ca.run_codex("run", self.d, "t", 活的, lambda r: ["codex"])
        self.assertLess(time.monotonic() - started, 0.3,
                        "等了——说明读的是参数那份，而那一份记的根本不是上一轮的写者")

    def test_同一进程连跑两轮不会等自己(self):
        """第二轮的磁盘元数据记的正是**本进程**（第一轮刚盖上去的）。

        直问「那个进程还在吗」就恒答「还在」——于是第二轮等自己，等满超时为止。
        真实失败模式是 **60 秒静默挂起**：等待排在分隔符之前，屏幕和日志都是死的，
        而「别让调用方遇到静默挂起」正是本工具存在的理由。
        挡住它的是 `_previous_writer_alive` 里「本进程不算」那一行。
        """
        with mock.patch.object(ca, "ROUND_END_TIMEOUT", 0.3), \
             mock.patch.object(ca, "find_codex_pid", return_value=None), \
             _no_codex():
            ca.run_codex("run", self.d, "t", _full_meta("t"), lambda r: ["codex"])
            落盘 = json.loads(ca.meta_path(self.d, "t").read_text())
            self.assertEqual((落盘["writer_pid"], 落盘["writer_start"]), ca._writer_identity(),
                             "前提不成立：第一轮没把本进程的身份盖上去，第二轮就不会等自己")
            started = time.monotonic()
            ca.run_codex("run", self.d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertLess(time.monotonic() - started, 0.3, "第二轮在等自己")

    def test_超时就拒绝_且不留半个状态(self):
        """**本版最值钱的性质**：等待排在 `write_meta` / `clear_report` / 分隔符
        **全部之前**，所以超时的时候，上一轮的报告还在、日志没被加分隔符、
        元数据还是上一轮那份、codex 一个都没起。
        """
        ca.write_meta(self.d, "t", _full_meta("t", **_live_writer(self)))
        报告 = ca._report_path(self.d, "t")
        报告.write_text("上一轮的报告")
        日志 = ca._log_path(self.d, "t")
        日志.write_text("旧日志\n")
        旧元数据 = ca.meta_path(self.d, "t").read_text()
        with mock.patch.object(ca, "ROUND_END_TIMEOUT", 0.3), \
             mock.patch.object(ca, "find_codex_pid", return_value=None), \
             _no_codex() as popen:
            with self.assertRaises(ca.Rejected) as cm:
                ca.run_codex("run", self.d, "t", _full_meta("t"), lambda r: ["codex"])
        self.assertEqual(cm.exception.code, 2, "护栏拒绝走 2，不许和判据结论撞码")
        popen.assert_not_called()
        self.assertEqual(报告.read_text(), "上一轮的报告", "clear_report 跑了")
        self.assertEqual(日志.read_text(), "旧日志\n", "分隔符写进去了")
        self.assertEqual(ca.meta_path(self.d, "t").read_text(), 旧元数据, "元数据被覆盖了")

    def test_超时文案按kind分(self):
        """两种停不下来的处置不同，而这行 stderr 是调用方唯一的线索。

        文案产生在 `run_codex` 内部，因为只有它认得 `kind`——而 `kind` 已经被
        `_require_enum(kind, _KINDS, "kind")` 守着，不会冒出第四种。
        """
        陪练 = _live_writer(self)
        for kind, 期望 in (("run", "换个任务名"), ("resume", "换个任务名"),
                           ("interrupt-and-resume", "不要再 stop")):
            with self.subTest(kind=kind):
                ca.write_meta(self.d, "t", _full_meta("t", **陪练))
                with mock.patch.object(ca, "ROUND_END_TIMEOUT", 0.3), \
                     mock.patch.object(ca, "find_codex_pid", return_value=None), \
                     _no_codex():
                    with self.assertRaises(ca.Rejected) as cm:
                        ca.run_codex(kind, self.d, "t", _full_meta("t"), lambda r: ["codex"])
                self.assertIn(期望, cm.exception.message)
                self.assertIn("包装器还在读", cm.exception.message, "没说清谁还没停")


class TestEveryInterruptPathLeavesAMark(_HomeSandbox):
    """三条打断路径必须都留痕。漏一条就会出现「stop 说可 resume、status 说 failed」。

    forward_as_sigint 那条由 TestSignalSafetyRealProcesses 用真信号钉着
    （它断言日志里出现 INTERRUPT_MARK），这里补 stop 这条——正是漏掉的那条。
    """

    def test_stop这条路也留痕(self):
        d = ca.ensure_isolation("default")
        ca.write_meta(d, "t", _full_meta("t"))
        log = ca._log_path(d, "t")
        # 非空日志，理由见 TestInterruptCodex.HEAD 上面那段
        head = ROUND_SEP_SAMPLE + "\nsession id: 01a0b408-f718-7ff3-8123-d5202551acba\n"
        log.write_text(head)
        args = ca.build_parser().parse_args(["stop", "t"])
        with mock.patch.object(ca, "find_codex_pid", return_value=4242), \
             mock.patch.object(ca.os, "kill") as k:
            ca.cmd_stop(args)
        k.assert_called_once_with(4242, signal.SIGINT)
        text = log.read_text()
        self.assertIn(ca.INTERRUPT_MARK, text,
                      "stop 只发信号不留痕 → status 会把被打断的轮次报成 failed")
        self.assertTrue(text.startswith(head), "stop 这条路把日志头部覆盖了")


class TestWrapperSpeaksImmediately(unittest.TestCase):
    """包装器自己说的话必须**当场**出现在屏幕上，不能攒到进程退出才吐。

    这条 fd 有两个写者：包装器的 print 走文本层，run_codex 的 tee 走
    sys.stdout.buffer（它自己 flush）。stdout 接管道／文件时文本层是**块缓冲**的，
    于是包装器的话会一直躺在缓冲区里，到退出才随 atexit 一起吐出来——排在
    codex 整轮输出**之后**。

    2026-09-19 端到端实测拿到过这个错序：`已发 SIGINT 并在日志留痕` 排在 codex
    整轮输出的最后面，而它要说的恰恰是「此刻正在发生什么」。最吃这条的是
    `_wait_previous_round_ends` 那句「上一轮还在收尾，最多等 N 秒」——它排在本轮
    分隔符之前，日志里此刻什么都没有，屏幕再不出声，那就是个彻头彻尾的静默挂起。

    **钉的是裸 `print`，不是某个助手函数。** 收一个 `_say()` 不解决问题，只是把
    软约定上移一层：每个调用点都得记得用它，新加一行裸 print 照样静默错序——
    实测那两个突变（新加裸 print、把某句 _say 改回裸 print）都**存活**。
    治本是在入口把文本层改成行缓冲，之后裸 print 自动正确。
    """

    def test_走过真实入口之后_裸print当场就读得到(self):
        d = pathlib.Path(tempfile.mkdtemp())
        screen = d / "screen.out"
        # 走 main() 这条真实入口（status 一个不存在的任务：只读、当场被护栏拒绝，main 自己接住返回 2），
        # 再用**裸 print**——不经任何助手函数。stdout 接的是文件不是 tty，
        # Python 默认块缓冲，所以「这行现在就读得到」只可能来自入口那行行缓冲配置。
        driver = (
            "import sys,time\n"
            f"sys.path.insert(0, {str(pathlib.Path(ca.__file__).parent)!r})\n"
            "import codex_agent as ca\n"
            "sys.argv = ['codex-agent', 'status', 'no-such-task-2026']\n"
            "ca.main()\n"
            "print('裸 print 这句必须当场看得到')\n"
            "time.sleep(30)\n"
        )
        with open(screen, "wb") as out:
            proc = subprocess.Popen([sys.executable, "-c", driver],
                                    stdout=out, stderr=subprocess.PIPE)
        try:
            deadline = time.time() + 8
            while time.time() < deadline and "裸 print 这句必须当场看得到" not in screen.read_text():
                if proc.poll() is not None:
                    self.fail(f"驱动进程提前退出：{proc.stderr.read().decode()}")
                time.sleep(0.05)
            self.assertIsNone(proc.poll(), "驱动进程已经退出了，那这条测的就不是实时性")
            self.assertIn("裸 print 这句必须当场看得到", screen.read_text())
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

    **它曾经偶发，而根因不在它自己身上**：它轮询 `tasks/t.json` 等 session_id
    落盘，而 `write_meta` 当时是 `Path.write_text`——先把目标截成 0 再填。
    读到半截就是一个裸 `JSONDecodeError`。2026-09-20 量过：1 秒里写 5289 次、
    并发读 12284 次，**8277 次读到半截**。`write_meta` 改成原子替换之后
    连跑 20 次零失败（见 TestWriteMetaIsAtomic）。
    **教训记在这里**：一条真进程测试偶发地红，先别归因到「机器慢」——
    那次差一点就把它当成调度抖动登记掉，而它照出来的是一个真的并发 bug。
    那几个 deadline 刻意不往上加：10 秒已经宽出两个量级，加大只会掩盖下一个
    这样的 bug，还让真的挂死多等好几秒。
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
        # 刻意留一个**没有换行结尾**的半行：read1(1024) 的边界是任意的，
        # codex 被 INT 截在半行是常态。痕迹要是接在它后面而不是行首，
        # 整行匹配就认不出来——下面的断言走 has_interrupt_mark 才照得到这件事。
        "sys.stdout.write('半行输出，没有换行')\n"
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
            text = log.read_text()
            # 走 has_interrupt_mark 而不是 `INTERRUPT_MARK in text`：子串断言
            # 正好绕开了要测的那件事（痕迹有没有落在行首）。假 codex 上面刻意
            # 留了个半行，所以这条现在真的照得到。
            self.assertTrue(ca.has_interrupt_mark(text),
                            "痕迹没落在行首，判据认不出这是一轮被打断的运行")
            # 痕迹必须**追加**：这条路的日志此刻已经有分隔符和 banner 了，
            # 去掉 O_APPEND 会从 0 覆盖写，把它们抹掉（session id 一没，
            # resume 再也回不来），而只断言「痕迹在里面」的话照样绿。
            self.assertTrue(text.startswith(ca.ROUND_MARK), "日志头部的分隔符被覆盖了")
            self.assertIn("session id:", text, "session id 被覆盖了，resume 再也回不来")
            self.assertGreater(text.index(ca.INTERRUPT_MARK), text.index("session id:"),
                               "痕迹落在了 banner 之前")
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            proc.stderr.close()
