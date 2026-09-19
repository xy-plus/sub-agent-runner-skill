import os
import pathlib
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


class TestCurrentRound(unittest.TestCase):
    def test_只扫最后一个分隔符之后_上一轮的错误不算这一轮的(self):
        log = (ca.round_separator("run", "t", "2026-09-19T10:00:00") + "\n" + ERR_FATAL + "\n"
               + ca.round_separator("resume", "t", "2026-09-19T11:00:00") + "\n干净收尾\n")
        self.assertEqual(ca.runtime_error_lines(log), [])

    def test_本轮自己的错误照样认(self):
        log = (ca.round_separator("run", "t", "2026-09-19T10:00:00") + "\n干净\n"
               + ca.round_separator("resume", "t", "2026-09-19T11:00:00") + "\n" + ERR_FATAL + "\n")
        self.assertEqual(len(ca.runtime_error_lines(log)), 1)

    def test_没有分隔符时扫全文_老日志和半路接手都还能判(self):
        self.assertEqual(len(ca.runtime_error_lines(ERR_FATAL)), 1)


class TestExtractSessionId(unittest.TestCase):
    def test_从带ANSI的日志头提取(self):
        self.assertEqual(ca.extract_session_id(HEADER),
                         "01a0b408-f718-7ff3-8123-d5202551acba")

    def test_还没打出来时返回None(self):
        self.assertIsNone(ca.extract_session_id("OpenAI Codex v0.154.0\n"))


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
        # 实测 156 份真实报告只有 4 份能解析成 JSON：-o 写的是 agent 的最后一条
        # 消息，通常是 markdown 散文。报告里该有什么字段是任务层的事，不是工具层的。
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
        self.log.write_text(ERR_FATAL)
        v = ca.judge(self.report, self.log, None)
        self.assertEqual(v.state, "failed")
        self.assertIn("锁", v.reason)

    def test_清报告之后旧内容不会被当成本轮产物(self):
        # codex 只在正常收尾时写 -o、启动时不 truncate。不清掉的话，一次秒死于
        # 写锁的 resume 会读到上一轮的报告并被判成 success——工具在说谎。
        self.report.write_text("上一轮的报告")
        ca.clear_report(self.report)
        self.assertEqual(ca.judge(self.report, self.log, None).state, "failed")

    def test_清报告对还没有报告的任务也成立(self):
        ca.clear_report(self.report)   # 不存在也不许抛


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
        # comm 过滤是承重的，不是保险。
        # 这里起一个 argv 里含该路径、comm 绝不是 codex 的活进程来验证它被排除。
        # （计划原稿用 `sleep 5 <mark>`，实测 sleep 会立刻以 "invalid time interval"
        #   退出——进程根本不存在，测试变成空转。所以先断言前提成立再断言结论。）
        mark = "/tmp/codex-agent-selftest-不存在的报告.json"
        proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep(30)  # {mark}"])
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                hit = subprocess.run(["pgrep", "-f", mark],
                                     capture_output=True, text=True).stdout.split()
                if str(proc.pid) in hit:
                    break
                time.sleep(0.05)
            else:
                self.fail("pgrep 没命中陪练进程，本测试无法验证 comm 过滤，不能算通过")
            self.assertIsNone(ca.find_codex_pid(mark))
        finally:
            proc.kill()
            proc.wait()
