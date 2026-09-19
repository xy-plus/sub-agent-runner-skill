import pathlib
import tempfile
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
