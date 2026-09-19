# 交给其他 agent 用：`--skill` 白名单与可切分的 `status` —— 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 spec 的两块改动。**改动 A（Task 1~4）**：`--skill`/`--no-skill` 白名单进 CLI，路径写错**当场拒跑**而不是让 codex 花 34.9 秒白跑一趟再被判成 `success`；兜底句从「固定常量 + 幂等前置」改成「**每轮派生 + 拒绝调用方自写**」；白名单进元数据。**改动 B（Task 5）**：`status` 列表形态换列序，让每行的 state 和退出码**机器切得开**，并把「切得开」从碰巧成立变成代码断言。**Task 6** 把 spec §4 的知识内化进代码注释，并把 `SKILL.md` 与测试模块 docstring 拉回与代码一致。

**Architecture:** 约束全部压到**入口**。`--dir`/`--skill` 各挂一个 argparse `type=` 函数（照 `task_name` 的形状），控制字符与路径三条校验在 `parse_args` 那一刻就拒；`--skill`/`--no-skill` 用 `add_mutually_exclusive_group(required=True)` 表达「二选一必填」，三条带 prompt 的命令各加一份。兜底句改由 `build_skill_guard(skill_paths)` **每轮派生**，`prepend_skill_guard` 收白名单并在发现调用方自己写了兜底句时 `reject`。白名单作为 `skills` 字段进元数据，与 `effort`/`started_at` 并列刷新。`status` 的数据行收进 `status_row()`，列序改成「任务名 账号 状态 退出码 工作目录+reason」，并在拼串之前断言前四列无空白、后两列无控制字符。

**Tech Stack:** Python 3 stdlib（`codex_agent.py` 不新增 import；`test_codex_agent.py` 新增 `import io`）。零第三方依赖。

## Global Constraints

- 对照 spec：`docs/superpowers/specs/2026-09-19-agent-facing-design.md`，每个决定以它为准。判据层级：**三条铁律 > 仓库规范 > 既有文档**（都是证据，不是权威）。
- 测试命令 `python3 -m unittest test_codex_agent -v`。**基线 177 个全绿**（已实跑确认）。每个任务给出「预期通过数」**并附加减账**（新增几条／迁移几条／删几条），数对不上就停下来核对，不要往下走。**终值 211。**
- **升级前置条件（做 Task 4 之前必须亲自确认）**：`_load_meta` 对缺 `skills` 的老元数据会拒绝并建议「删掉它重新 run」——那会把会话弄丢。2026-09-19 实测三个隔离目录 `tasks/` 全空（`~/.codex-subagent`、`~/.codex-subagent-acct2`、`~/.codex-subagent-acct3` 各 0 个 json），所以现实影响为零。**动手前再跑一次那条命令确认，非空就先停下来问。**
- **空测试比没测试更糟。** 两条硬规矩：① 凡依赖外部文件／进程／权限的测试，**先断言前提成立**（本计划里权限 000、控制字符目录、账号名带空格三处都有对应的前提断言，一条都不许省）；② 凡「断言常量等于那个常量自己」一律视为空测试，要用**绝对值**（退出码列钉 `0/1/3/4/130` 的字面量，不许写 `ca.EXIT[state]`；元数据字段清单钉那七个名字的字面量）。这条本仓栽过**五次**，前四次写在 `test_codex_agent.py` 的模块 docstring 里，第五次（`f9dd0ef`：反向判据只断言「某个词出现过」，把 effort 分档表整张删掉照样绿）还没补进去——Task 6 负责补。动测试之前先读那段 docstring。
- **不设任何默认缺省值，包括函数默认参数。** `new_meta` 的第五个参数、`prepend_skill_guard` 的第二个参数、`_resume_round` 的第七个参数都必须由调用方显式传。`--skill`/`--no-skill` 二选一必填，正是这条规矩在 CLI 上的形式。
- **import 一律并到文件头**，两个文件都是。函数体里不许出现 `import`。
- 凡需要 HOME 沙箱的测试类**一律继承已有的 `_HomeSandbox`**，不许再手写一份。纯函数测试不要它。
- **知识写进代码注释，贴在防住它的那行旁边**，不另开文档文件。spec 里这四个实测数字必须落到对应注释里，一个都不许漏：
  | 数字 | 落点 |
  |---|---|
  | codex 找不到 skill 时 `find` 跑了 **34.9 秒**、**一件事没干**、工具报 **exit 0** | `skill_path()` 的 docstring |
  | 权限 000 的文件 `is_file()` 返回 **True** 而 `cat` 退 **1** | `skill_path()` 里 `os.access` 那一行旁边 |
  | 制表符方案实测**四行零列对齐**（`[0,16,24,32,40,80]`／`[0,16,24,32,40,88]`／`[0,8,16,24,32,40]`／`[0,32,40,56,64,80]`，而空格定宽稳定在 `[0,25,34,43]`） | `status_row()` 上方 |
  | `--dir` 可以含制表符和换行，`mkdir`／`resolve()`／`is_dir()` **全过** | `_reject_control_chars()` 的 docstring |
- 测试函数名用**中文**，沿用现有风格（`test_只认comm是codex的进程_别的进程不算`）。
- 每个任务末尾有**突变复验**步骤：把实现故意改坏、跑测试确认对应的那条**真的红**、再还原。改坏之后全绿 = 那条测试是空的，当场补。
- 每个任务提交一次，提交信息中文、说清楚**为什么**。提交时**不传 `-c user.email` / `-c user.name`**，也不设 `GIT_AUTHOR_*`／`GIT_COMMITTER_*`，用仓库已配好的身份。

---

### Task 1: 两个入口校验函数（控制字符 + `--skill` 路径三条）

先做纯函数，照 `task_name` 的形状：`type=` 函数 + `argparse.ArgumentTypeError`。这一步不碰 parser，所以不会把既有测试染红，可以单独验干净。

**Files:** Modify `codex_agent.py` · Modify `test_codex_agent.py`

**Interfaces:**
- Consumes: 现有 `task_name`（形状样板）、`re`／`os`／`pathlib`／`argparse`（已在文件头）
- Produces（确切签名）：
  - `_CONTROL_CHARS: re.Pattern` —— `[\x00-\x1f\x7f]`
  - `_reject_control_chars(flag: str, value: str) -> None` —— 命中就 `raise argparse.ArgumentTypeError`
  - `work_dir(value: str) -> str` —— `--dir` 的 `type=`，只管控制字符
  - `skill_path(value: str) -> str` —— `--skill` 的 `type=`，控制字符 + 绝对 + 是文件 + 可读，四条缺一不可

- [ ] **Step 1: 写失败的测试**

`test_codex_agent.py` 里，紧跟在 `class TestTaskName` 之后加两个类：

```python
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
        p = d / "SKILL.md"
        p.write_text("---\nname: x\n---\n")
        return p

    def test_收一个正常的绝对路径SKILL文件(self):
        p = self._skill_file()
        self.assertTrue(p.is_absolute() and p.is_file(), "前提不成立：样本文件没建起来")
        self.assertEqual(ca.skill_path(str(p)), str(p))

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
        p = self._skill_file()
        os.chmod(p, 0o000)
        try:
            self.assertTrue(p.is_file(), "前提不成立：is_file 本来就该返回 True，这条才有意义")
            self.assertFalse(os.access(p, os.R_OK), "前提不成立：这个文件居然读得了")
            self.assertEqual(subprocess.run(["cat", str(p)], capture_output=True).returncode, 1,
                             "前提不成立：codex 用的 cat 居然没退 1")
            with self.assertRaises(argparse.ArgumentTypeError) as cm:
                ca.skill_path(str(p))
            self.assertIn("读不了", str(cm.exception))
        finally:
            os.chmod(p, 0o644)

    def test_传目录被拒_错误信息要说清期望的是文件本身(self):
        # 传 skill 目录是最容易犯的错，而 is_file() 为 False 时调用方看不出为什么
        d = self._dir()
        self.assertTrue(d.is_dir(), "前提不成立：目录没建起来")
        with self.assertRaises(argparse.ArgumentTypeError) as cm:
            ca.skill_path(str(d))
        self.assertIn("SKILL.md", str(cm.exception))

    def test_含控制字符的路径被拒(self):
        p = self._skill_file()
        for bad in (f"{p}\t", f"{p}\n", f"/abs\x00/SKILL.md"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError) as cm:
                ca.skill_path(bad)
            self.assertIn("控制字符", str(cm.exception))

    def test_每条拒绝都点名是哪个路径(self):
        # 调用方是 agent：它拿到的只有 stderr 那一行，不点名就无从改起
        p = self._skill_file()
        os.chmod(p, 0o000)
        try:
            cases = ["skills/foo/SKILL.md", str(self._dir() / "SKILL.md"),
                     str(p.parent), str(p)]
            for bad in cases:
                with self.subTest(bad=bad):
                    with self.assertRaises(argparse.ArgumentTypeError) as cm:
                        ca.skill_path(bad)
                    self.assertIn(bad, str(cm.exception))
        finally:
            os.chmod(p, 0o644)


class TestWorkDirArg(unittest.TestCase):
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
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError) as cm:
                ca.work_dir(bad)
            self.assertIn("控制字符", str(cm.exception))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestSkillPathArg test_codex_agent.TestWorkDirArg -v`

Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'skill_path'`（`work_dir` 同）

- [ ] **Step 3: 最小实现**

`codex_agent.py` 里，紧跟在 `def task_name(value):` 那个函数之后插入：

```python
# 控制字符（C0 全段 + DEL）。制表符和换行只是其中最容易撞上的两个。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _reject_control_chars(flag, value):
    """控制字符必须在**入口**挡住，不能指望「一般没人这么干」。

    2026-09-19 实测：`--dir` 给一个含制表符和换行的路径，`mkdir`／`resolve()`／
    `is_dir()` **全都放行**——文件系统这一层根本不管。而这个路径会原样进
    `status` 的数据行，一个换行就让「一行一任务」不成立，任何切分方案都救不
    回来（见 status_row）。
    `--skill` 同理，但坏法不同：兜底句把白名单**逐行**列出来，路径里一个换行
    就把一条白名单静默劈成两行，codex 读到的是两个都不存在的路径。
    """
    hit = _CONTROL_CHARS.search(value)
    if hit:
        # 这里用 !r：value 已经确定含控制字符，裸插进错误信息会把 stderr 也弄成
        # 多行／带制表符的一坨。skill_path 后面三条的 value 是干净路径，用裸的。
        raise argparse.ArgumentTypeError(
            f"{flag} {value!r} 含控制字符 {hit.group()!r}（第 {hit.start()} 个字符）。"
            f"这个值要原样进 status 的数据行和兜底句的白名单行，控制字符会把它们切坏。")


def work_dir(value):
    """`--dir` 的 type=。只管控制字符；「是不是目录」归 cmd_run——它要先 expanduser／resolve。"""
    _reject_control_chars("--dir", value)
    return value


def skill_path(value):
    """`--skill` 的 type=。四条缺一不可，全部当场拒，绝不「尽力而为」地继续。

    收的是 **SKILL.md 文件本身**，不是 skill 目录。

    为什么非得当场拒：2026-09-19 真跑（`--effort low`，brief 指向一个不存在的
    SKILL.md）——codex 第一步 `cat` 退 1，第二步拿 `find` 翻真实 home **跑了
    34.9 秒**，结论「未找到该文件，因此无法严格按其流程执行，尚未创建 out.txt」，
    磁盘上产物**不存在**，而本工具判 `success`、**退出码 0**。
    「零工作量」被报成「完成」，且没有任何别的信号救得回来：`cat` 的失败是 shell
    退出码，不匹配 runtime_error_lines 的三种错误形式，judge 结构上看不见它。
    """
    _reject_control_chars("--skill", value)
    p = pathlib.Path(value)
    if not p.is_absolute():
        raise argparse.ArgumentTypeError(
            f"--skill {value} 不是绝对路径。codex 的 cwd 是 --dir，相对路径解释不出你的意思。")
    if not p.is_file():
        raise argparse.ArgumentTypeError(
            f"--skill {value} 不是文件。要传的是 SKILL.md **文件本身**，不是 skill 目录。")
    # 第三条是审查实测逼出来的：**权限 000 的文件 is_file() 返回 True**，
    # 而 codex 的 `cat` 退 1。只查前两条的话，这次改动的核心承诺（路径写错从
    # 静默失效变当场报错）在「文件存在但读不了」这一支上原样漏掉。
    if not os.access(p, os.R_OK):
        raise argparse.ArgumentTypeError(
            f"--skill {value} 存在但当前用户读不了（权限 {oct(p.stat().st_mode)[-3:]}）。"
            f"codex 的 cat 会退 1，而那个失败判据看不见。")
    return value
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`

Expected: PASS，**186 个全绿**。加减账：177 ＋ 7（`TestSkillPathArg`）＋ 2（`TestWorkDirArg`）− 0 ＝ 186。

- [ ] **Step 5: 突变复验**（三个突变逐个做，做完还原）

1. 删掉 `skill_path` 里 `os.access` 那一支 → `test_权限000的文件被拒_is_file说True而codex的cat退1` 必须红
2. `_CONTROL_CHARS` 改成 `re.compile(r"[\x00]")`（只挡 NUL）→ `test_含控制字符的路径被拒` 与 `test_含制表符或换行的目录被拒_而mkdir和is_dir全都放行` 必须红
3. 删掉 `is_absolute` 那一支 → `test_相对路径被拒` 必须红

三条都红 = 四条校验真的各有人守。任何一条绿，当场补。

- [ ] **Step 6: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: --skill 与 --dir 的入口校验——路径写错当场拒，不留给 codex 去发现

skill_path 四条缺一不可：控制字符 / 绝对 / 是文件 / 可读。第四条是实测逼出来的，
权限 000 的文件 is_file() 返回 True 而 codex 的 cat 退 1，少查一条核心承诺就漏掉。
控制字符单独抽出来共用：文件系统对含 \\t \\n 的路径 mkdir/resolve/is_dir 全放行，
挡不住它就没有任何 status 切分方案救得回来。"
```

---

### Task 2: 三条命令各加 `--skill`/`--no-skill` 互斥必填，并把 22 处既有 argv 迁移过去

**这一步会把 37 条既有测试一次性染红**（已实跑确认：`TestRunGuards` 11、`TestExitCodeContract` 11、`TestResumeGuards` 7、`TestRoundBoundaryWiring` 2、`TestSkillGuardIsAlwaysPrepended` 2、`TestInterruptAndResumeParser` 2、`TestParser` 1、`TestMetaShape` 1），所以参数表和迁移必须在**同一个任务**里做完。

还有一类更阴的：那些 `assertRaises(SystemExit)` 的参数表测试**不会红，但会变成空测试**——它们本来验的是「少了 `--effort` 会被拒」，现在少了 `--skill` 也被拒，删不删那个参数都 SystemExit。这类一条都不许放过。

**Files:** Modify `codex_agent.py` · Modify `test_codex_agent.py`

**Interfaces:**
- Consumes: Task 1 的 `work_dir` / `skill_path`
- Produces（确切签名）：
  - `_add_prompt_round_args(sub: argparse.ArgumentParser) -> None` —— 把「白名单二选一必填」这一组挂到一个子命令上，三条带 prompt 的命令共用
  - 解析结果：`args.skills` 恒为 `list[str]`——`--no-skill` 给 `[]`，`--skill` 按给定顺序 append。**永远不是 `None`**（组是 required 的）

- [ ] **Step 1: 写失败的测试**

① 新增一个类，放在 `class TestInterruptAndResumeParser` 之后：

```python
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
        for p in (self.skill_a, self.skill_b):
            p.write_text("---\nname: x\n---\n")

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
                self.assertEqual(parser.parse_args(self._argv(cmd, "--no-skill")).skills, [])
                with self.assertRaises(SystemExit):
                    parser.parse_args(self._argv(cmd, "--skill", str(self.skill_a), "--no-skill"))

    def test_no_skill解析成空白名单(self):
        parser = ca.build_parser()
        for cmd in self.PROMPT_CMDS:
            with self.subTest(cmd=cmd):
                self.assertEqual(parser.parse_args(self._argv(cmd, "--no-skill")).skills, [])

    def test_skill可重复且保持给定顺序(self):
        parser = ca.build_parser()
        for cmd in self.PROMPT_CMDS:
            with self.subTest(cmd=cmd):
                args = parser.parse_args(self._argv(
                    cmd, "--skill", str(self.skill_a), "--skill", str(self.skill_b)))
                self.assertEqual(args.skills, [str(self.skill_a), str(self.skill_b)])

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
                self.assertEqual(parser.parse_args(good).skills, [str(self.skill_a)],
                                 "前提不成立：好的那条都过不去，坏的被拒就说明不了任何事")
                with self.assertRaises(SystemExit):
                    parser.parse_args(self._argv(cmd, "--skill", "relative/SKILL.md"))
```

② **迁移 22 处既有 argv**。做法统一：在对应子命令的 argv 末尾加 `"--no-skill"`。逐处列出（行号以当前 HEAD 为准）：

| # | 位置 | 处理 |
|---|---|---|
| 1 | `TestRoundBoundaryWiring.test_run收尾用的是run_codex回传的本轮文本` :492 | `"--account", "default", "--no-skill"]` |
| 2 | `TestRoundBoundaryWiring.test_resume收尾用的是run_codex回传的本轮文本` :505 | `"--effort", "low", "--no-skill"]` |
| 3 | `TestMetaShape.test_run落盘的元数据键集与校验面相等_不多不少` :967 | 同 #1 |
| 4 | `TestParser.test_run的五个参数一个都不能少` :1201 | 基准 argv 加 `"--no-skill"`；**不加就退化成空测试**（删不删都 SystemExit） |
| 5 | `TestParser.test_effort五个档位都收` :1212 | 同 #1 |
| 6 | `TestParser.test_effort只收这五个档位` :1222 | 同 #1（空测试风险同 #4） |
| 7 | `TestParser.test_任务名校验挂在五个子命令上_结构上绕不过` :1228,:1230 | `run`／`resume`／`interrupt-and-resume` 三条 argv 各加；`status`／`stop` 不动 |
| 8 | `TestParser.test_resume和stop不收account_账号是查出来的` :1237 | 加（空测试风险同 #4） |
| 9 | `TestParser.test_不提供会造成误用的参数` :1258 | 在 `bad` 之前加（空测试风险同 #4） |
| 10 | `TestInterruptAndResumeParser.test_与resume同一张参数表_少一个都不收` :1269 | 加（空测试风险同 #4） |
| 11 | `TestInterruptAndResumeParser.test_effort五档正反都验` :1280,:1285 | 正反两处都加 |
| 12 | `TestInterruptAndResumeParser.test_不收account_账号是查出来的` :1290 | 加（空测试风险同 #4） |
| 13 | `TestInterruptAndResumeParser.test_不提供任何旋钮_没有第二种正确行为` :1303 | 在 `bad` 之前加（空测试风险同 #4） |
| 14 | `TestInterruptAndResumeParser.test_子命令接到的确实是这条命令的实现` :1309 | 加 |
| 15 | `TestRunGuards._args` :1324 | 加（一处覆盖 10 条测试） |
| 16 | `TestRunGuards.test_dir相对路径被转成绝对_相对路径启动即崩` :1374 | 加 |
| 17 | `TestResumeGuards._args` :1448 | 加（一处覆盖 7 条测试） |
| 18 | `TestSkillGuardIsAlwaysPrepended.test_run这条路` :1715 | 加（Task 3 会整条重写，这里先让它绿） |
| 19 | `TestSkillGuardIsAlwaysPrepended.test_resume这条路` :1724 | 同上 |
| 20 | `TestExitCodeContract._run_args` :1749 | 加（一处覆盖 4 条） |
| 21 | `TestExitCodeContract._resume_args` :1753 | 加（一处覆盖 3 条） |
| 22 | `TestExitCodeContract._interrupt_and_resume` :1811 | 加（一处覆盖 4 条） |

③ `TestParser.test_不提供会造成误用的参数` 的 docstring 表格**不动**——那六个旋钮的理由没变。

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestSkillWhitelistFlags -v`

Expected: FAIL，**7 条里 6 条挂**，逐条对得上才算写对：

| 测试 | 这一刻的结果 |
|---|---|
| `test_三条带prompt的命令都必须二选一_都不给就拒` | `AssertionError: SystemExit not raised`（argv 本来就完整） |
| `test_三条命令都不许同时给` | `SystemExit: 2`（正面控制那半就先挂了，`--no-skill` 还不认） |
| `test_no_skill解析成空白名单` | `SystemExit: 2`（`unrecognized arguments: --no-skill`） |
| `test_skill可重复且保持给定顺序` | `SystemExit: 2` |
| `test_dir的控制字符校验真的挂在命令行上` | `SystemExit: 2`（正面控制那半挂） |
| `test_skill的路径校验真的挂在命令行上` | `SystemExit: 2`（正面控制那半挂） |
| `test_status和stop不收这两个参数_它们不带prompt` | **绿**——它守的是「别顺手给 status 也加上」，本来就该前后都绿 |

**没有正面控制的话，后四条在这一刻全是绿的**（两个参数都还 `unrecognized`，坏值当然被拒）——那就是四条永远不会红的空测试。写测试时别把正面控制那半删掉。

- [ ] **Step 3: 最小实现**

`codex_agent.py` 的 `build_parser` 正上方插入：

```python
def _add_prompt_round_args(sub):
    """带 prompt 的三条命令共用的「本轮 skill 白名单」。**二选一必填，没有默认值。**

    白名单是**每一轮**的事，不是任务的事——resume 换一轮活，能用的 skill 就该
    跟着换，所以三条命令各收一份，而不是在 run 时定死。

    为什么 `--no-skill` 必须显式写：这个工具要交给其他 agent 用。省略时分不清
    「调用方决定不给」和「调用方根本不知道有这个参数」，而 argparse 的
    `required=True` 能把后者变成 exit 2 当场报错。代价很小——这个拒绝是即时且
    完全可恢复的（加个参数重跑，零损失），不像 --effort/--account 写错要花钱
    才发现。

    **刻意不把 skill 软链进隔离目录**，理由是可观测性：路径在 brief 里，
    「codex 到底读没读」在日志里看得见（就是那一行 `cat <路径>`）；软链成能力
    之后，用没用由它决定、**不可观测**。对一个主张「不骗调用方」的工具，这条是
    决定性的。第二条是奥卡姆：软链要求隔离目录从「每账号一个」变成「每任务
    一个」，isolation_home／find_meta／account_choices／ensure_isolation 全线
    要改，换来的保证是零。
    """
    g = sub.add_mutually_exclusive_group(required=True)
    g.add_argument("--skill", action="append", dest="skills", type=skill_path,
                   metavar="SKILL_MD", help="允许 codex 读的 SKILL.md 绝对路径，可重复")
    g.add_argument("--no-skill", action="store_const", const=[], dest="skills",
                   help="本轮一个 skill 都不给")
```

`build_parser` 里四处改动：

```python
    r.add_argument("--dir", required=True, type=work_dir, help="codex 的工作目录，自动转绝对路径")
```
```python
    r.add_argument("--account", required=True, choices=account_choices(), help="codex 账号")
    _add_prompt_round_args(r)
    r.set_defaults(func=cmd_run)
```
```python
    m.add_argument("--effort", required=True, choices=EFFORTS)
    _add_prompt_round_args(m)
    m.set_defaults(func=cmd_resume)
```
```python
    j.add_argument("--effort", required=True, choices=EFFORTS)
    _add_prompt_round_args(j)
    j.set_defaults(func=cmd_interrupt_and_resume)
```

并把 `run` 那段的注释从「五个参数全必填」改成：

```python
    # 五个带值参数全必填，外加 --skill/--no-skill 二选一：不设默认值，因为隐式
    # 选中的账号／难度／白名单都是最容易被误用的地方
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`

Expected: PASS，**193 个全绿**。加减账：186 ＋ 7（`TestSkillWhitelistFlags`）＝ 193。迁移的 22 处**不改变条数**（它们覆盖的 37 条测试从红回绿）。

- [ ] **Step 5: 自检 CLI**

```bash
python3 codex_agent.py run --help
python3 codex_agent.py resume --help
python3 codex_agent.py interrupt-and-resume --help
python3 codex_agent.py status --help
```

Expected: 前三条的 usage 行里有 `(--skill SKILL_MD | --no-skill)`；`status` 里**没有**。

- [ ] **Step 6: 突变复验**（三个，做完还原）

1. `_add_prompt_round_args` 的 `required=True` 改成 `required=False` → `test_三条带prompt的命令都必须二选一_都不给就拒` 必须红
2. 只给 `run` 调 `_add_prompt_round_args`，`resume`／`interrupt-and-resume` 那两行删掉 → 同一条的 `resume`／`interrupt-and-resume` 两个 subTest 必须红
3. `r.add_argument("--dir", ...)` 的 `type=work_dir` 去掉 → `test_dir的控制字符校验真的挂在命令行上` 必须红

- [ ] **Step 7: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: --skill/--no-skill 二选一必填，三条带 prompt 的命令各收一份

白名单是每一轮的事，不是任务的事，所以 resume 和 interrupt-and-resume 也收。
必填而不是缺省成空：这个工具要交给其他 agent 用，省略时分不清「决定不给」和
「根本不知道有这个参数」，而后者只有 exit 2 才拦得住。
同时迁移 22 处既有 argv——其中 8 处若不迁移不会红，只会静默退化成空测试
（少不少 --effort 都 SystemExit）。"
```

---

### Task 3: 兜底句每轮派生，调用方自己写就拒跑

现有 `SKILL_GUARD` 是常量，`prepend_skill_guard` 靠 `startswith(SKILL_GUARD)` 去重。句子一旦每轮派生，这个幂等闸就不成立了。

**拒绝比去重少一个分支，且把「无定义」变成「不可能」**：两句兜底句并存时的优先级根本不该需要被定义（铁律 2）。而 SKILL.md 把那句话明文印着、调用方照抄进 brief 开头是**可达路径**。

**Files:** Modify `codex_agent.py` · Modify `test_codex_agent.py`

**Interfaces:**
- Consumes: Task 2 的 `args.skills`
- Produces（确切签名）：
  - `SKILL_GUARD_STEM: str` —— `"不得使用任何 skill"`，两种派生措辞与旧常量共有的词干
  - `build_skill_guard(skill_paths: list) -> str`
  - `prepend_skill_guard(brief_text: str, skill_paths: list) -> str` —— 拒绝 + 派生 + 前置，焊死
  - `_resume_round(kind, home, meta, task, brief_path, effort, skills) -> int` —— 加第七个参数
- Removes: `SKILL_GUARD` 常量 —— 它不再是「每轮同一句话」，留着就是第二个家

- [ ] **Step 1: 写失败的测试**

① 删掉 `TestArgv.test_兜底句被前置且只加一次`（:798-801）整条——幂等语义已经不存在了。

② 新增纯函数类，放在 `class TestArgv` 之前：

```python
class TestSkillGuardIsDerivedPerRound(unittest.TestCase):
    """兜底句**每轮派生**，且**只能由工具写**。

    旧常量那半句「除非本 brief 明确指定」本来就是「CLI 没有这个参数」的变通：
    调用方无处声明白名单，只好让 brief 正文去破例。--skill 出现之后那半句就该
    消失——白名单由 CLI 指定，brief 正文不再是声明渠道。

    断言的是**字面量**，不是 ca.SKILL_GUARD_STEM 拼出来的串：后者两边会一起动，
    把措辞整个改掉测试照样绿（本仓在这上面栽过五次）。
    """

    def test_无白名单时的措辞(self):
        self.assertEqual(ca.build_skill_guard([]), "**不得使用任何 skill。**")

    def test_有白名单时逐行列出每条绝对路径(self):
        self.assertEqual(
            ca.build_skill_guard(["/abs/one/SKILL.md", "/abs/two/SKILL.md"]),
            "**不得使用任何 skill，以下几个除外：**\n"
            "- /abs/one/SKILL.md\n"
            "- /abs/two/SKILL.md")

    def test_brief自带兜底句就拒跑_并提示改用参数(self):
        with self.assertRaises(ca.Rejected) as cm:
            ca.prepend_skill_guard("**不得使用任何 skill。**\n\n干活", [])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--no-skill", cm.exception.message)
        self.assertIn("--skill", cm.exception.message)

    def test_旧那句兜底句也算自带_SKILL_md历史上印过它(self):
        # 调用方照抄 SKILL.md 开头那句是**可达路径**，不拒就会出现两句互相
        # 矛盾的兜底句。字面量写死那句旧话：它已经不在代码里了，只能这么钉。
        with self.assertRaises(ca.Rejected):
            ca.prepend_skill_guard("**不得使用任何 skill，除非本 brief 明确指定。**\n\n干活", [])

    def test_SKILL_GUARD常量已经不存在_它不再是每轮同一句话(self):
        self.assertFalse(hasattr(ca, "SKILL_GUARD"),
                         "常量留着就是第二个家：派生一份、常量一份，两份必然漂移")
```

③ 把 `class TestSkillGuardIsAlwaysPrepended` 整类改写（原 2 条 → 4 条）：

```python
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
        self.assertTrue(brief.startswith("**不得使用任何 skill，以下几个除外：**"),
                        f"codex 实际收到的是：{brief[:60]!r}")
        self.assertIn(f"- {self.skill}", brief)
        self.assertIn("干活", brief)

    def test_resume这条路_有白名单(self):
        self._meta_for_resume()
        args = ca.build_parser().parse_args(
            ["resume", "t", "--brief", str(self.brief), "--effort", "low",
             "--skill", str(self.skill)])
        with mock.patch.object(ca, "find_codex_pid", return_value=None):
            brief = self._brief_codex_actually_got(lambda: ca.cmd_resume(args))
        self.assertTrue(brief.startswith("**不得使用任何 skill，以下几个除外：**"),
                        f"codex 实际收到的是：{brief[:60]!r}")
        self.assertIn(f"- {self.skill}", brief)

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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestSkillGuardIsDerivedPerRound test_codex_agent.TestSkillGuardIsAlwaysPrepended -v`

Expected: FAIL，**9 条全挂**，逐条对得上才算写对：

| 测试 | 这一刻的结果 |
|---|---|
| `test_无白名单时的措辞`、`test_有白名单时逐行列出每条绝对路径` | `AttributeError: module 'codex_agent' has no attribute 'build_skill_guard'` |
| `test_brief自带兜底句就拒跑_并提示改用参数`、`test_旧那句兜底句也算自带_SKILL_md历史上印过它` | `TypeError: prepend_skill_guard() takes 1 positional argument but 2 were given` |
| `test_SKILL_GUARD常量已经不存在_它不再是每轮同一句话` | `AssertionError: True is not false` |
| `TestSkillGuardIsAlwaysPrepended` 四条 | `AssertionError: False is not true : codex 实际收到的是：'**不得使用任何 skill，除非本 brief 明确指定。**\n\n干活'`——旧常量那半句还在，而且 `args.skills` 还没人接 |

- [ ] **Step 3: 最小实现**

`codex_agent.py` 里把 `SKILL_GUARD` 常量和 `prepend_skill_guard` 整段换掉：

```python
# 兜底句的**词干**。派生出来的两种措辞都含有它，SKILL.md 历史上印过的那句
# 「**不得使用任何 skill，除非本 brief 明确指定。**」也含有它——所以拿它当
# 「调用方是不是自己写了兜底句」的判据，一条就挡住全部写法。
SKILL_GUARD_STEM = "不得使用任何 skill"
```

```python
def build_skill_guard(skill_paths):
    """本轮的兜底句。**每轮派生，不是常量**——白名单是每一轮的事。

    旧常量那半句「除非本 brief 明确指定」本来就是「CLI 没有这个参数」的变通：
    调用方无处声明白名单，只好让 brief 正文去破例。`--skill` 出现之后那半句就
    该消失——白名单由 CLI 指定，brief 正文不再是声明渠道。

    白名单**逐行**列出，所以路径里一个换行就能把一条静默劈成两条
    （见 _reject_control_chars，那条拒绝就是为这里守的）。
    """
    if not skill_paths:
        return f"**{SKILL_GUARD_STEM}。**"
    return f"**{SKILL_GUARD_STEM}，以下几个除外：**\n" + "\n".join(f"- {p}" for p in skill_paths)


def prepend_skill_guard(brief_text, skill_paths):
    """兜底句前置。CODEX_HOME 隔离是结构性防线，这句是内容层的第二道。

    **调用方自己写了兜底句就拒跑**，不去重、不合并：两句并存时的优先级根本不
    该需要被定义（铁律 2）。而 SKILL.md 把那句话明文印过、调用方照抄进 brief
    开头是**可达路径**。拒绝比去重少一个分支，且把「无定义」变成「不可能」。

    这条拒绝**留在信号之后也没关系**（interrupt-and-resume 会先发 INT 再走到
    这里）。按 check_can_resume 写下的判据分类，它是**可恢复**的：上下文还在，
    改掉 brief 再 `resume` 一次就送达，而那一轮的截断本来就是调用方点名要的。
    不可恢复的那几道（没 session id／工作目录没了／brief 不是文件）仍然排在
    信号之前，一条都没动。
    """
    if SKILL_GUARD_STEM in brief_text:
        reject(f"brief 里已经有兜底句（含「{SKILL_GUARD_STEM}」）。这句话归工具所有：\n"
               f"要放行哪些 skill 就用 --skill 逐条给（SKILL.md 的绝对路径，可重复），"
               f"一个都不给就用 --no-skill。")
    return f"{build_skill_guard(skill_paths)}\n\n{brief_text}"
```

`cmd_run` 里那两行改成：

```python
    brief = prepend_skill_guard(brief_file.read_text(), args.skills)
    # 不再打印兜底句：它每轮派生、有白名单时是多行，而「这一轮给了哪些 skill」
    # 的权威副本在元数据的 skills 字段里（见 new_meta）。印第二份只会漂移。
```

`_resume_round` 加第七个参数（**无默认值**），并在它已有的 docstring 末尾补一句
「`skills` 是本轮的白名单，和 effort 一样每轮重给——白名单是每一轮的事」。
函数体只动 `brief = ...` 这一行（`meta["skills"]` 的刷新归 Task 4）：

```python
def _resume_round(kind, home, meta, task, brief_path, effort, skills):
    """（docstring 原样保留，末尾补上面那句）"""
    check_can_resume(task, meta, brief_path)
    ensure_isolation(meta["account"])
    brief = prepend_skill_guard(pathlib.Path(brief_path).expanduser().read_text(), skills)
```

两个调用点：

```python
    return _resume_round("resume", home, meta, args.task, args.brief, args.effort, args.skills)
```
```python
    return _resume_round("interrupt-and-resume", home, meta, args.task, args.brief,
                         args.effort, args.skills)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`

Expected: PASS，**199 个全绿**。加减账：193 − 1（删 `TestArgv.test_兜底句被前置且只加一次`）＋ 5（`TestSkillGuardIsDerivedPerRound`）＋ 2（`TestSkillGuardIsAlwaysPrepended` 从 2 条变 4 条）＝ 199。

- [ ] **Step 5: 突变复验**（三个，做完还原）

1. `prepend_skill_guard` 的拒绝改回去重（`if SKILL_GUARD_STEM in brief_text: return brief_text`）→ `test_brief自带兜底句就拒跑_并提示改用参数` 与 `test_旧那句兜底句也算自带_SKILL_md历史上印过它` 必须红
2. `build_skill_guard` 里 `if not skill_paths` 改成 `if True`（永远返回无白名单那句）→ `test_有白名单时逐行列出每条绝对路径` 与 `test_run这条路_有白名单时路径真的进了argv` 必须红
3. `cmd_interrupt_and_resume` 传 `[]` 而不是 `args.skills` → `test_interrupt_and_resume这条路_无白名单` **不会**红（它本来就传空），所以改成传 `["/x/SKILL.md"]` → 那条必须红。这个突变本身就说明：**只测无白名单那一支是不够的**，两种措辞各一条是承重的。

- [ ] **Step 6: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: 兜底句每轮派生，调用方自己写就拒跑

--skill 出现之后，旧常量里那半句「除非本 brief 明确指定」就该消失：它本来
就是「CLI 没有这个参数」的变通，让 brief 正文去破例。
幂等闸跟着一起没了——句子每轮不同，startswith 去重不成立。改成拒绝而不是
去重：少一个分支，且两句并存时的优先级根本不该需要被定义。
SKILL_GUARD 常量删除，留着就是第二个家。"
```

---

### Task 4: 白名单进元数据（`skills`），与 `effort`/`started_at` 并列刷新

沿用既有原则「元数据描述的是**最后一次调用**」，不让实现者自己发明 resume 时用哪一轮的。

**动手前先确认升级条件**（Global Constraints 里那条）：`for d in ~/.codex-subagent*; do ls -1 "$d/tasks" | wc -l; done` 必须全是 0。

**Files:** Modify `codex_agent.py` · Modify `test_codex_agent.py`

**Interfaces:**
- Consumes: Task 3 的 `_resume_round(..., skills)`
- Produces（确切签名）：
  - `new_meta(task: str, account: str, workdir: str, effort: str, skills: list) -> dict` —— 第五个位置参数，**无默认值**
  - `REQUIRED_META_KEYS` —— 从 `new_meta("", "", "", "", [])` 派生，自动多一个 `skills`
  - `_load_meta` **不改**：它的校验面从 `REQUIRED_META_KEYS` 派生，一处改全处到

- [ ] **Step 1: 写失败的测试**

① 迁移三处：

| 位置 | 改成 |
|---|---|
| `_full_meta` :111-112 | 字典里加 `"skills": []` |
| `TestMetaShape.FIELDS` :948 | 七个名字的字面量：`{"task", "account", "dir", "effort", "skills", "session_id", "started_at"}` |
| `TestMetaShape.test_构造器的键集就是校验面` :958 | `ca.new_meta("t", "default", "/abs/x", "low", [])` |

`TestMetaShape.test_字段清单的绝对值` 里再加一条：

```python
        # 白名单是「最后一次调用给了什么」，要审计就读这个字段——它刻意不进
        # status 的列：skill 路径是任意长度的绝对路径，进数据行会把格式撑坏。
        self.assertIn("skills", self.FIELDS)
```

② 新增三条，放进 `class TestMetaShape`：

```python
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
        for p in (first, second):
            p.write_text("---\nname: x\n---\n")
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestMetaShape -v`

Expected: FAIL，**5 条挂**：

| 测试 | 这一刻的结果 |
|---|---|
| `test_字段清单的绝对值` | `AssertionError: Items in the second set but not the first: 'skills'` |
| `test_构造器的键集就是校验面` | `TypeError: new_meta() takes 4 positional arguments but 5 were given` |
| `test_run落盘的skills就是命令行给的那几条` | `KeyError: 'skills'`（`new_meta` 还没这个字段） |
| `test_resume刷新skills_元数据描述的是最后一次调用` | `AssertionError: ['…/a_SKILL.md'] != ['…/b_SKILL.md']`——字段在（`_full_meta` 塞进去的），但**没被刷新** |
| `test_interrupt_and_resume也把skills接了进去` | `AssertionError: [] != ['…/c_SKILL.md']`——同上 |

- [ ] **Step 3: 最小实现**

`codex_agent.py` 四处：

```python
def new_meta(task, account, workdir, effort, skills):
    """元数据的**唯一**构造器。字段清单只在这里写一次。

    刻意没有 pid：存活必须每次重新反查，存下来的 PID 会过期、还会被系统复用，
    留着它只会诱导别人犯这个设计本来要防的错。

    `skills` 记的是**最后一次调用**的白名单，与 effort／started_at 同一条原则
    （见 _resume_round）。它是白名单唯一的结构化副本——刻意不进 status 的列：
    skill 路径是任意长度的绝对路径，进数据行会把定宽格式撑坏，要审计就读这里。
    """
    return {"task": task, "account": account, "dir": workdir, "effort": effort,
            "skills": skills, "session_id": None, "started_at": _now_iso()}
```

```python
REQUIRED_META_KEYS = tuple(new_meta("", "", "", "", []).keys())
```

`cmd_run` 里 `new_meta(...)` 那一处：

```python
                              new_meta(args.task, args.account, str(workdir),
                                       args.effort, args.skills),
```

`_resume_round` 里刷新那三行：

```python
    # 元数据描述的是**最后一次调用**：effort、白名单、开跑时间一起刷新。
    # 完整的轮次历史不在这里，在日志的分隔符里（每轮一行，带时间戳）。
    meta["effort"] = effort
    meta["skills"] = skills
    meta["started_at"] = _now_iso()
```

同时把 `test_codex_agent.py` 模块 docstring 里那句「字段清单钉那六个名字」改成「那**七**个名字」——同一条事实，同一个提交里改完。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`

Expected: PASS，**202 个全绿**。加减账：199 ＋ 3（`TestMetaShape` 新增三条）＝ 202。迁移的三处不改变条数。

- [ ] **Step 5: 突变复验**（两个，做完还原）

1. `_resume_round` 里删掉 `meta["skills"] = skills` → `test_resume刷新skills_元数据描述的是最后一次调用` 与 `test_interrupt_and_resume也把skills接了进去` 必须红
2. `new_meta` 里删掉 `"skills": skills` → `test_字段清单的绝对值` 必须红（`test_构造器的键集就是校验面` **不会**红——两边一起动，这正是 `FIELDS` 写成字面量的理由）

- [ ] **Step 6: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: 白名单进元数据，与 effort/started_at 并列刷新

沿用「元数据描述的是最后一次调用」这条既有原则，不让实现者自己发明 resume
时该记哪一轮的。REQUIRED_META_KEYS 从构造器派生，一处改全处到。
字段清单的绝对值从六个名字改成七个——那条断言写死字面量，正是因为
「构造器的键集 == 校验面」两边会一起动。
升级前已确认三个隔离目录 tasks/ 全空，不存在被拒绝的老元数据。"
```

---

### Task 5: `status` 换列序，并把「机器切得开」变成代码断言

```
<任务名>  <账号>  <状态>  <退出码>  <工作目录及其后的 reason>
                                     ↑ split(maxsplit=4) 在这里停
```

**零新格式、人读体验一点不退化、少一个改动点。** 代价是 `dir` 与 `reason` 不可分——可以接受：`dir` 已经在 `tasks/<task>.json` 里，而调用方真正要的是 state 和退出码。

**Files:** Modify `codex_agent.py` · Modify `test_codex_agent.py`

**Interfaces:**
- Consumes: 现有 `EXIT`、`Verdict`、Task 1 的 `_CONTROL_CHARS`
- Produces（确切签名）：
  - `_WHITESPACE: re.Pattern` —— `\s`
  - `status_row(task: str, account: str, state: str, workdir: str, reason: str) -> str` —— 拼数据行，拼之前先断言
- 测试文件新增 `import io`（并到文件头，排在 `contextlib` 和 `json` 之间）

- [ ] **Step 1: 写失败的测试**

新增一个类，放在 `class TestExitCodeContract` 之前：

```python
class TestStatusIsSplittable(_HomeSandbox):
    """列表形态的 status 必须**机器切得开**。这是本改动的全部理由。

    旧格式是 `f"{task:<24} {account:<8} {state:<8} {reason}  {dir}"`，而 reason
    含空格 → 后面任何一列都取不出来。**7 个 Verdict 构造点里有 5 个的 reason
    真的含空格**（「本轮被 INT 打断，上下文保留——接着 resume 即可，不用重跑」、
    「报告在，但本轮日志有 N 条未分类的 codex 错误」、「…曾被 SIGTERM 杀过…」、
    「pid=N 存活」，以及 suspect 那条）。
    注意 spec §1 举的那个例子（「报告缺失或为空＝没正常收尾」）**其实不含空格**
    ——实跑逐条核过。结论不变，例子要换成真的那几条，否则这条测试是空的。

    真正缺的只有一样：列表形态下每行的 state（单任务查询用退出码就够了，
    dir/account/effort/session_id 早就在 tasks/<task>.json 里）。

    采纳的方案是**换列序、不换格式**。初稿的制表符方案被实测否掉：按 tabstop=8
    量四行真实 status 输出的各列屏幕起始列，[0,16,24,32,40,80] /
    [0,16,24,32,40,88] / [0,8,16,24,32,40] / [0,32,40,56,64,80]——四行没有一列
    对齐，而空格定宽是稳定的 [0,25,34,43]。
    """

    REASON_WITH_SPACES = "报告缺失或为空＝没正常收尾 还带了空格"

    def test_前四列用split切得开_reason含空格也不影响(self):
        row = ca.status_row("t1", "default", "failed", "/abs/repo", self.REASON_WITH_SPACES)
        self.assertGreater(len(self.REASON_WITH_SPACES.split()), 1,
                           "前提不成立：reason 不含空格的话，这条根本测不到东西")
        self.assertEqual(row.split(maxsplit=4)[:4], ["t1", "default", "failed", "1"])

    def test_第五段是工作目录加reason_dir含空格也切得开(self):
        row = ca.status_row("t1", "default", "failed", "/abs/my repo", self.REASON_WITH_SPACES)
        tail = row.split(maxsplit=4)[4]
        self.assertTrue(tail.startswith("/abs/my repo"), tail)
        self.assertTrue(tail.endswith(self.REASON_WITH_SPACES), tail)

    def test_退出码那一列的绝对值_五态逐个(self):
        # 绝对值，不写 ca.EXIT[state]：那样两边一起动，EXIT["running"]=0 这种
        # 突变照样绿——而那正是 status && deploy 提前部署的那个 bug。
        for state, code in (("success", "0"), ("failed", "1"), ("suspect", "3"),
                            ("running", "4"), ("interrupted", "130")):
            with self.subTest(state=state):
                row = ca.status_row("t1", "default", state, "/abs/repo", "一句人话")
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
            ca.status_row("t1", "default", "failed", "/abs/repo", "坏\treason")
        self.assertEqual(cm.exception.code, 2)

    def test_工作目录含换行时当场拒绝(self):
        with self.assertRaises(ca.Rejected):
            ca.status_row("t1", "default", "failed", "/abs/a\nb", "一句人话")

    def test_账号名含空格时当场拒绝_choices来自任意目录名(self):
        # 前四列「天生无空格」这句话对账号**不成立**：account_choices() 扫的是
        # ~/.codex-accounts/ 下的任意目录名，谁建一个带空格的，第二列就把切分冲垮。
        (self.home / ".codex-accounts" / "bad acct").mkdir(parents=True)
        self.assertIn("bad acct", ca.account_choices(),
                      "前提不成立：带空格的账号名没被扫进来，这条测的就不是真的洞")
        with self.assertRaises(ca.Rejected):
            ca.status_row("t1", "bad acct", "failed", "/abs/repo", "一句人话")

    def test_任务名和状态含空白也拒绝(self):
        for task, state in (("t 1", "failed"), ("t1", "fai led")):
            with self.subTest(task=task, state=state), self.assertRaises(ca.Rejected):
                ca.status_row(task, "default", state, "/abs/repo", "一句人话")

    def test_真实status输出每行一个任务_两个任务各切出四列(self):
        # 刻意选 interrupted 这一支：它的 reason（「本轮被 INT 打断，上下文保留
        # ——接着 resume 即可，不用重跑」）**真的含空格**，端到端走一遍才算把
        # 「reason 排在最后」这件事钉住。选 failed 那一支测不到——它的 reason
        # 一个空格都没有（spec §1 的例子在这点上是错的）。
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
```

并在 `test_codex_agent.py` 文件头加 `import io`（排在 `import contextlib` 之后、`import json` 之前）。

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent.TestStatusIsSplittable -v`

Expected: FAIL，**9 条里 8 条挂**：

| 测试 | 这一刻的结果 |
|---|---|
| 直接调 `status_row` 的那 7 条 | `AttributeError: module 'codex_agent' has no attribute 'status_row'` |
| `test_真实status输出每行一个任务_两个任务各切出四列` | `AssertionError: ['default', 'interrupted', '本轮被'] != ['default', 'interrupted', '130']`——旧列序下第四段是 reason 的头一个词 |
| `test_明细行有缩进_数据行没有` | **绿**——前三列在旧格式里位置没变，缩进本来就有。它是**保持性**测试：换列序这次重构最容易顺手弄坏的就是它，而突变复验第 4 条会把它打红 |

- [ ] **Step 3: 最小实现**

`codex_agent.py` 里，`_print_verdict` 正上方插入：

```python
# status 数据行的列序是**机器切分的契约**：前四列天生无空格，所以
# `line.split(maxsplit=4)` 精确切出它们，第五段是「工作目录 + reason」。
# **7 个 Verdict 构造点里有 5 个的 reason 含空格**（「本轮被 INT 打断，上下文
# 保留——接着 resume 即可，不用重跑」、「报告在，但本轮日志有 N 条未分类的
# codex 错误」、「…曾被 SIGTERM 杀过…」、「pid=N 存活」、写锁那条），
# 所以 reason 必须排在最后——这就是这次换列序的全部理由。
# dir 与 reason 因此不可分，可以接受：dir 早就在
# tasks/<task>.json 里，调用方真正要的是 state 和退出码。
#
# **刻意不换格式。** 初稿的制表符方案被实测否掉：按 tabstop=8 量四行真实输出
# 的各列屏幕起始列，[0,16,24,32,40,80] / [0,16,24,32,40,88] / [0,8,16,24,32,40]
# / [0,32,40,56,64,80]——四行没有一列对齐；而空格定宽是稳定的 [0,25,34,43]。
#
# 退出码单独成列：它和 state 是同一份事实的两种编码，但**同源派生**（都来自
# EXIT[state]），不存在漂移风险。人读词，机器读码。
#
# 白名单（skills）刻意**不进列**：skill 路径是任意长度的绝对路径，进数据行会
# 把定宽撑坏。要审计就读 tasks/<task>.json，那本来就是结构化的。
_WHITESPACE = re.compile(r"\s")


def status_row(task, account, state, workdir, reason):
    """拼一行数据行，**拼之前先断言它切得开**——这条不是碰巧成立的。

    前四列断言「一个空白都没有」：`split(maxsplit=4)` 靠的就是它。看着天生
    无空格（任务名过 _TASK_NAME、状态是枚举、退出码是整数），但**账号不是**
    ——`account_choices()` 扫的是 `~/.codex-accounts/` 下的任意目录名，谁建一个
    带空格的目录，第二列当场把切分冲垮。

    后两列断言「没有控制字符」：dir 和 reason 里空格是合法的（它们同在第五段），
    换行和制表符不是——一个换行就让「一行一任务」不成立，任何切分方案都救不
    回来。`--dir` 在入口已经挡过一道（见 work_dir），reason 当前也恰好不含
    （穷举 Verdict 的 7 个构造点确认过），但那是**碰巧成立、无人守卫**。

    明细行（错误行与报告预览）不走这里：它们由 cmd_status 加缩进打印，
    首字符是空白，从而与数据行结构性可分；而它们的内容来自 splitlines()，
    结构上不可能含换行。
    """
    for label, field in (("任务名", task), ("账号", account), ("状态", state)):
        if _WHITESPACE.search(field):
            reject(f"{label} {field!r} 含空白字符——status 的前四列就切不开了，"
                   f"调用方再也取不出 state。")
    for label, field in (("工作目录", workdir), ("reason", reason)):
        if _CONTROL_CHARS.search(field):
            reject(f"{label} {field!r} 含控制字符——「一行一任务」就不成立了。")
    return f"{task:<24} {account:<8} {state:<8} {EXIT[state]:<4} {workdir}  {reason}"
```

`cmd_status` 里打印那三行改成：

```python
        print(status_row(meta["task"], meta["account"], verdict.state,
                         meta["dir"], verdict.reason))
        for line in verdict.detail:
            # 缩进保持：首字符是空白 → 与数据行结构性可分，调用方不必猜哪行是任务
            print(f"    {line}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`

Expected: PASS，**211 个全绿**。加减账：202 ＋ 9（`TestStatusIsSplittable`）＝ 211。既有的 `test_status_还在跑退出4` 等只断言退出码，换列序不影响它们。

- [ ] **Step 5: 自检真实输出**

```bash
python3 codex_agent.py status
```

Expected: 「还没有任何任务」（三个隔离目录 `tasks/` 全空），退出码 0。有任务时肉眼确认前四列对齐、明细行缩进。

- [ ] **Step 6: 突变复验**（四个，做完还原）

1. `status_row` 的返回值改回旧列序 `f"{task:<24} {account:<8} {state:<8} {reason}  {workdir}"` → `test_前四列用split切得开_reason含空格也不影响` 与 `test_真实status输出每行一个任务_两个任务各切出四列` 必须红
2. 删掉前四列的空白断言那个 for 循环 → `test_账号名含空格时当场拒绝_choices来自任意目录名` 与 `test_任务名和状态含空白也拒绝` 必须红
3. 删掉 dir/reason 的控制字符断言那个 for 循环 → `test_reason含制表符时当场拒绝` 与 `test_工作目录含换行时当场拒绝` 必须红
4. `cmd_status` 的明细行从 `f"    {line}"` 改成 `f"{line}"` → `test_明细行有缩进_数据行没有` 必须红

- [ ] **Step 7: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: status 换列序，把「机器切得开」变成代码断言

reason 含空格，排在中间就让后面任何一列都取不出来。换成
任务名/账号/状态/退出码/(工作目录+reason)，split(maxsplit=4) 精确切前四列。
零新格式：制表符方案实测四行零列对齐，而空格定宽稳定在 [0,25,34,43]。
断言不是装饰——account_choices() 扫的是任意目录名，前四列「天生无空格」
这句话对账号本来就不成立。"
```

---

### Task 6: 内化知识 + 把 `SKILL.md` 和测试模块 docstring 拉回一致

三份文档现在都在说假话：`SKILL.md` 的「给它 skill」「brief」两行讲的是没有 `--skill` 的世界；测试模块 docstring 的「兜底句两条路」少一条路、「栽过四次」少一次、「61 行」会变；`ensure_isolation` 只说「防自激活」，既没说在挡什么具体的东西，也没说哪条路已经试过了。

**判据是「泄漏数 = 0」，不是行数**——`SKILL.md` 只写人／模型才能决定的事。

**Files:** Modify `codex_agent.py` · Modify `SKILL.md` · Modify `test_codex_agent.py`

**Interfaces:**
- Consumes: `TestSkillDocDoesNotRepeatCode.OWNED_BY_CODE`（把它改严）
- Produces: 无新函数

- [ ] **Step 1: 先把文档测试改严（它现在还不该绿）**

`TestSkillDocDoesNotRepeatCode.OWNED_BY_CODE` 里加一条：

```python
        r"不得使用任何 skill": "build_skill_guard（兜底句由 --skill/--no-skill 每轮派生）",
```

`test_文档仍然保留代码替不了的那部分` 末尾加：

```python
        # 白名单二选一必填是**对外契约**，代码替不了调用方决定给哪几个 skill
        for flag in ("--skill", "--no-skill"):
            with self.subTest(flag=flag):
                self.assertIn(flag, skill)
```

- [ ] **Step 2: 跑泄漏测试确认它红**

Run: `python3 -m unittest test_codex_agent.TestSkillDocDoesNotRepeatCode -v`

Expected: FAIL，两条都红。`test_没有一条代码级约束泄漏进文档` 报「这些约束已经由代码保证，文档里不该再说一遍：不得使用任何 skill → 归 build_skill_guard…」（SKILL.md 现在正写着「工具会自动前置「不得使用任何 skill」并打印一行提示」）；`test_文档仍然保留代码替不了的那部分` 报 `'--skill' not found`。

- [ ] **Step 3: 改 `SKILL.md` 四处**

① 启动那一行与它下面那句：

```bash
codex-agent run --task <任务名> --dir /abs/repo --brief brief.md --effort low --account default --no-skill
```
```
五个带值参数**全必填**，外加 `--skill`/`--no-skill` 二选一，没有默认值。
```

② 「另外四条命令」代码块里，`resume` 和 `interrupt-and-resume` 两行末尾各加 `--no-skill`；并在代码块下面补一句（**说性质，不说机制**——不写 `split`，那是调用方自己推得出来的）：

```
`status` 不带任务名时**一行一个任务**：前四列（任务名／账号／状态／退出码）都不含空格，
第五段起是工作目录和一句人话；**缩进的行是明细，不是任务**。
```

③ 「调用方仍需要知道的三件事」表里两行：

| 事 | 改成 |
|---|---|
| brief | 只收**文件路径**，不收内联字符串。**兜底句归工具所有，不要自己写进 brief**——写了当场拒跑 |
| 给它 skill | `--skill <SKILL.md 的绝对路径>`，可重复；一个都不给就 `--no-skill`，二选一必填。**路径写错当场拒跑**，不会再静默跑出一个「零工作量的成功」。不要往共享目录里放东西 |

**注意泄漏**：不复述兜底句的措辞（那已经归 `build_skill_guard`），不写「三条校验是哪三条」（归 `skill_path`），不写 `split(maxsplit=4)`（归 `status_row`）。

- [ ] **Step 4: 跑泄漏测试确认转绿**

Run: `python3 -m unittest test_codex_agent.TestSkillDocDoesNotRepeatCode -v`

Expected: PASS，2 条绿。

- [ ] **Step 5: 内化 spec §4 的知识到 `ensure_isolation`**

`codex_agent.py` 里把 `ensure_isolation` 的 docstring 换成：

```python
    """保证隔离目录满足全部不变量，不满足就拒跑（而不是"尽力而为"地继续）。

    **它挡的是动机，不是能力**——这条必须写明白，否则下一个人会拿它去推错结论。
    2026-09-19 实测：`collaboration.spawn_agent` 等六个工具**恒在**，
    `--disable multi_agent` **无效**（加与不加，codex 报的工具清单逐字相同）；
    `skip_host_skill_discovery` 也不影响服务端那 5 个 skill（两次清单逐字吻合）。
    真正被挡住的是**主目录那一侧**：23 个 skill、2 个 MCP、3 个 hook 全部看不见。
    服务端那 5 个与「想去编排」无关，**刻意不管**（关它们是解决不存在的问题）。
    `~/.agents/skills` 放哨兵文件确实会被 codex 列出来——所以下面那条
    「共享扫描根非空即拒跑」是**承重的**，不是防御性编程。

    顺带记下一条被审查推翻的错理由：曾经写过「不把 skill 软链进来，是因为
    codex 看见流程类 skill 就想去编排」——**对精选集不成立**（只软链
    test-driven-development 时，codex 够不着 subagent-driven-development，
    那个口子打不开）。不软链的真理由在 _add_prompt_round_args 里：可观测性 +
    奥卡姆。留着一条错理由比没有理由更危险。
    """
```

- [ ] **Step 6: 同步测试模块 docstring**

`test_codex_agent.py` 顶部三处：

① 「承重约束」清单最后一条改成，并追加两条：

```
    退出码**五态**的绝对值、严重度排序（interrupted 在 running 之后）、元数据字段清单
    read1 的实时性、stdout flush、EPERM 即存活、strip_ansi（resume 路上承重）
    resume 的三处 flag 差异、任务名字符集、兜底句**三条路**都真的加上且**每轮派生**
    控制字符在入口就挡住（--dir / --skill，文件系统那一层根本不管）
    status 数据行前四列无空白、明细行有缩进——「机器切得开」是断言出来的，不是碰巧
```

② 「这条是四次踩出来的」改成「**五次**」，并在第 4 条之后补第 5 条：

```
    5. `test_文档仍然保留代码替不了的那部分` 只断言「某个词出现过」。把 effort
       分档表整张删掉，`--effort low` 那行还在，`effort` 这个词照样搜得到——
       实测这条突变**存活**过。反向判据也要钉具体内容。
```

③ 「行数（现在 61 行）」改成实测值——跑 `wc -l SKILL.md` 拿数字，别猜。

- [ ] **Step 7: 全量测试**

Run: `python3 -m unittest test_codex_agent -v`

Expected: PASS，**211 个全绿**。加减账：211 ＋ 0 ＝ 211（本任务只改严既有的 2 条，不新增）。

- [ ] **Step 8: 突变复验**（两个，做完还原）

1. 把兜底句原文（`**不得使用任何 skill。**`）写回 `SKILL.md` → `test_没有一条代码级约束泄漏进文档` 必须红
2. 把 `SKILL.md` 里「给它 skill」那一行的 `--skill`／`--no-skill` 删掉 → `test_文档仍然保留代码替不了的那部分` 必须红

- [ ] **Step 9: 提交**

```bash
git add codex_agent.py test_codex_agent.py SKILL.md
git commit -m "docs: SKILL.md 与测试 docstring 拉回与代码一致，隔离知识内化进代码

SKILL.md 三处在说假话：兜底句的措辞（已归 build_skill_guard，泄漏测试加了
一条模式守着）、「在 brief 里写绝对路径」（已归 --skill）、status 的行结构。
ensure_isolation 补上「挡的是动机不是能力」：--disable multi_agent 实测无效、
六个 collaboration 工具恒在、服务端 5 个 skill 刻意不管；顺带钉死一条被审查
推翻过的错理由，免得下一个人拿它去推别的结论。
测试模块 docstring 的「栽过四次」补成五次——第五次（f9dd0ef，反向判据只断言
某个词出现过）一直没进那张清单。"
```

---

## 收尾验收（全部任务做完之后）

- [ ] `python3 -m unittest test_codex_agent -v` → **217 个全绿**（计划写的 211 是
  按计划原样做完的数；实际 +6：spec 要求把兜底句检查前移，配一条「绝不发信号」的闸序
  测试；spec §3 要求 `account_choices()` 拒坏目录名，配两条（拒绝本身 + 拒绝要说人话）；
  审查实跑补出 `--dir` 软链绕过、`--no-skill` 的 const 共享、`status_row` 的状态枚举各一条）
- [ ] `python3 -m py_compile codex_agent.py test_codex_agent.py` 零警告（`TestModuleCompilesClean` 已经守着，这里是人工复核）
- [ ] `grep -n "SKILL_GUARD\b" codex_agent.py SKILL.md` → 无输出。**不要把
  `test_codex_agent.py` 列进来**：那里有一条合法命中（`test_SKILL_GUARD常量已经不存在`）
- [ ] 四个实测数字**逐条独立 grep**，任一失败即停。**不要写成一条
  `grep -c "34.9\|000\|0,25,34,43\|mkdir"`**：`mkdir` 本来就命中两次，
  动手之前那条就已经是 4，而 `0,25,34,43` 一次不命中也照样是 4——恒真，等于没查：
  ```bash
  for k in "34.9" "权限 000" "0,25,34,43" "mkdir"; do
      grep -q "$k" codex_agent.py || { echo "缺：$k"; break; }
  done
  ```
- [ ] spec §5 点名的三条既有测试都已迁移：`:799-801`（幂等 → 删除，语义不存在了）、`:1717`／`:1727`（argv 级 `startswith(SKILL_GUARD)` → 改成断言派生出的两种措辞的字面量）
- [ ] 四者对齐复核：spec 的每一条 ↔ 计划的每一个 Task ↔ 代码与注释 ↔ `SKILL.md`。确认**做完了**再谈删 spec 与本计划（判据是「事情真的做完了」，不是「我认为内化完了」）
