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
