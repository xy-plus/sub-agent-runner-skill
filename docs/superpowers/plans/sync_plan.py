"""把计划文档里的代码块／测试块从真实源文件重新生成。

计划文档里的代码是源文件的**投影**，不是第二份真相。三轮审查里已经因为
「两边各改一遍」漂移过三次，所以不再手工对着打补丁，一律跑这个脚本。
"""
import pathlib, sys

plan_p = pathlib.Path("docs/superpowers/plans/2026-09-19-codex-agent-cli.md")
plan = plan_p.read_text()
code = pathlib.Path("codex_agent.py").read_text()
tests = pathlib.Path("test_codex_agent.py").read_text()

def slice_src(src, start, end):
    a = src.index(start)
    b = len(src) if end is None else src.index(end, a)
    return src[a:b].rstrip("\n")

# 每个 Task 对应源文件里的一段。边界都用「下一段的第一行」，在源文件里唯一。
IMPL = {
    1: slice_src(code, "# 护栏拒绝走独立退出码", "def clear_report(report_path):"),
    2: slice_src(code, "def clear_report(report_path):", 'MODEL = "gpt-6-astra"'),
    3: slice_src(code, 'MODEL = "gpt-6-astra"', 'SKILL_GUARD = '),
    4: slice_src(code, 'SKILL_GUARD = ', "def meta_path(home, task):"),
    5: slice_src(code, "def meta_path(home, task):", "# 四个状态，按「最该放行"),
    6: slice_src(code, "# 四个状态，按「最该放行", None),
}
TEST = {
    1: slice_src(tests, "class TestStripAnsi", "class TestJudge"),
    2: slice_src(tests, "class TestJudge", "class TestIsolation"),
    3: slice_src(tests, "class TestIsolation", "class TestArgv"),
    4: slice_src(tests, "class TestArgv", "class TestMeta(_HomeSandbox):"),
    5: slice_src(tests, "class TestMeta(_HomeSandbox):", "class TestTaskName"),
    6: slice_src(tests, "class TestTaskName", None),
}

TASK_HEADS = {n: plan.index(f"### Task {n}:") for n in range(1, 9)}
out = []
for n in range(1, 7):
    head, nxt = TASK_HEADS[n], TASK_HEADS[n + 1]
    body = plan[head:nxt]
    s1 = body.index("- [ ] **Step 1")
    s2 = body.index("- [ ] **Step 2")
    s3 = body.index("- [ ] **Step 3")
    s4 = body.index("- [ ] **Step 4")
    new_body = (
        body[:s1]
        + "- [ ] **Step 1: 写失败的测试**\n\n"
          "> 本块由 `/tmp/sync_plan.py` 从 `test_codex_agent.py` 生成，别手工改这里。\n"
          "> 顶部的 `_no_codex()` / `_full_meta()` / `_HomeSandbox` 是全文件共用的助手。\n\n"
          "```python\n" + TEST[n] + "\n```\n\n"
        + body[s2:s3]
        + "- [ ] **Step 3: 最小实现**\n\n"
          "> 本块由 `/tmp/sync_plan.py` 从 `codex_agent.py` 生成，别手工改这里。\n"
          "> `import` 全部在文件头，不在本块里。\n\n"
          "```python\n" + IMPL[n] + "\n```\n\n"
        + body[s4:]
    )
    out.append((head, nxt, new_body))

for head, nxt, new_body in reversed(out):
    plan = plan[:head] + new_body + plan[nxt:]
plan_p.write_text(plan)
print(f"已重建 Task 1~6 的 12 个块，plan 现 {len(plan.splitlines())} 行")
