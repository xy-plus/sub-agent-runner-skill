# codex-agent CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `codex exec` 的 12 条软约束编译进一个可执行文件，让调用方只提供任务信息（干什么／在哪干／多难），其余全部由工具保证。

**Architecture:** 单文件 Python CLI（`codex_agent.py`），四个子命令 `run/status/resume/stop`。纯函数（ANSI 剥离、ERROR 识别、session id 提取、判据、argv 组装）与副作用（进程、文件系统）分离，纯函数全部单测覆盖。启动仍由调用方用 `Bash(run_in_background: true)` 执行，保留 harness 的完成通知。

**Tech Stack:** Python 3（仅 stdlib：`argparse`／`subprocess`／`unittest`／`re`／`json`／`pathlib`）。

## Global Constraints

- 对照 spec：`docs/superpowers/specs/2026-09-19-codex-agent-cli-design.md`，每个决定以 spec 为准。
- **零第三方依赖**，只用 Python3 stdlib。
- **不设任何默认缺省值**：`run` 的五个参数全必填；内部函数也不写默认参数值，由调用方显式传常量。
- 模型固定 `MODEL = "gpt-6-astra"`，难度只由 `--effort` 分档。
- 退出码：`0` success、`1` failed、`3` suspect（**不用 2**，argparse 的参数错误占用了 2）。
  实测确认后台任务的完成通知里带退出码（exit 3 的通知写的就是 failed with exit code 3），
  所以「run 的退出码＝判据结论」这个核心收益成立。
- 报告文件是 `reports/<任务>.md`：`-o` 写的是 agent 的最后一条**消息**，实测 156 份里
  只有 4 份是 JSON，其余都是 markdown 散文（P1-1）。
- 文件：`codex_agent.py`、`test_codex_agent.py`，均在仓库根目录。测试命令 `python3 -m unittest test_codex_agent -v`。
- 每个任务结束提交一次，提交信息中文、说清楚为什么。
- **注释承载知识**：spec 与旧 SKILL.md 里的实测教训（日期＋当时怎么炸的）写进代码注释，贴在防住它的那行旁边，不另开文档。
- **import 一律并到文件头**，Task 1 就写齐（`argparse` `datetime` `json` `os` `pathlib`
  `re` `signal` `subprocess` `sys` + `typing.NamedTuple`）。按任务分散写会散落十处。
- **护栏拒绝一律用 `reject()`，退出码 2**，不用裸 `raise SystemExit("人话")`——后者的
  退出码是 1，和 `EXIT["failed"]` 撞码，调用方分不清「`--dir` 写错了」和「codex 真的失败了」。


## 第三轮：代码审查（78 个突变，杀 57 存活 21）的处置

审查确认承重约束的突变全部被杀（信号安全、`comm`/`-u` 过滤、防陈旧报告、三种错误形式、
target 白名单、开跑前三件事的顺序、`read1` 实时性、EPERM、`strip_ansi`、severity 排序、
resume 三处 flag 差异、任务名字符集、四个子命令的 `type=`）。存活的 21 个集中在下面几块。

| 编号 | 问题 | 处置 |
|---|---|---|
| C1 | 退出码契约**零覆盖**，且 `test_还在跑时退出码不是0` 断言的是 `EXIT["running"] == EXIT["running"]`，两边一起动，名字在说谎 | 钉死 `EXIT` 的**绝对值**；`cmd_run`／`cmd_resume` 各补 success／failed／suspect 三态退出码测试；全文件扫一遍同类空断言 |
| I1 | `_TASK_NAME` 允许 `.`，而 `.` 是 `pgrep -f` 的正则元字符：任务名 `a` 会命中 `aXmd` 的 codex，`stop a` 把 SIGINT 发给别人 | **不转义，治本**：`find_codex_pid` 改成扫 `/proc/*/cmdline` 做 **argv 精确元素匹配** + 读 `comm` + `st_uid`。一次干掉正则语义、`-u`、`comm` 三件事，还省掉每次 1+N 个子进程 |
| I2 | `run_codex` 收现成的 `argv`，却自己重新推导报告路径去删——三条「必须记得对齐」无人保证 | `run_codex(kind, home, task, meta, make_argv)`，报告路径由它拥有并回传给 `make_argv`；`env` 也由 `home` 内部派生。三条契约变成结构上不可违反 |
| I3 | `REQUIRED_META_KEYS` 号称「形状只定义一次」，`cmd_run` 却另起炉灶拼字典；少一个字段 → 任务从此 status/resume/stop 全够不着，而工具建议「删掉重来」会**弄丢会话** | 唯一构造器 `new_meta()`，`REQUIRED_META_KEYS` **由它派生**；测试断言落盘 meta 的键集**相等** |
| I4 | 七条硬约束无测试：`approval_policy`／`project_doc_max_bytes`／`--disable plugins`／`--skip-git-repo-check`／`--dir` 不再 resolve／`stdin=DEVNULL`／`cmd_resume` 的 `ensure_isolation` | 一张参数断言表对两条 argv **各跑一遍**（含 m10 的 `MODEL`）；补 resolve、stdin、resume 隔离校验三条测试 |
| I5 | 被 INT 打断的任务判据报 `failed`，而正确处置是 `resume`——「`run_in_background` 编不进去」那条缓解措施自己的缺口 | `forward_as_sigint` 往日志追一行打断标记，`judge` 在「报告缺失」分支特判，**只改 reason 不新增状态** |
| I6 | 「SIGINT 之后仍可 resume」是 2026-09-08 的旧结论，而同类假设本分支已漂移过一次（`--color`） | 真机 `run → stop → resume` 复验一次 `--effort low` |
| m11 | `finally` 还原信号处置被判「不可达」 | **不接受**：单测是在进程内直调 `run_codex` 的第二调用方，不还原则测试进程余生响应不了 Ctrl-C。代码保留，**补测试钉住还原** |

**I4 最后一条的更正（实测）**：审查说「`cmd_resume` 不调 `ensure_isolation`，是实现违反 spec §6」。
实测不成立——`cmd_resume` 第 627 行就有这一句，删掉它 80 个测试照样全绿。
所以这是**测试缺口，不是代码缺陷**，只补测试，不改代码。

**m4**（`status` 无任务返回 0）：保持 0——查询没查到不是失败——但补测试钉住，别让它是「碰巧」。
**m9**（`SKILL.md` 55 行 > 计划写的 ≤50）：**行数不是验收判据**，真判据是「已进代码的约束在文档里泄漏数 = 0」（实测为 0）。
不为凑数字删内容，下面 Task 7 的验收条件按真判据改写。

### I6 真机复验结果（2026-09-19，codex 0.154.0）：**旧结论成立**

`run` → `stop`（只发 SIGINT）→ `resume` 跑通，一次 `--effort low`：

| 证据 | 结果 |
|---|---|
| 打断时 | `status` 报 `running`（退出码 4），pid=3754898，s1.txt 已创建、s2/s3 未做 |
| `stop` | 「已向 i6-sigint-recheck (pid=3754898) 发 SIGINT，上下文保留，可 resume」，包装器退出码 1 |
| session id | 两轮**完全相同**：`01a0b93d-c24e-7a60-92b4-5172aafc5a1d` |
| token 计数 | 3,216 → 3,989，**接着涨**，没有重新计数 |
| **上下文本身** | 追问「被打断前你成功创建了哪几个文件」，codex 答：「`s1.txt`。`s2.txt` 当时尚未确认完成，`s3.txt` 未执行」——完全正确，它知道自己被打断在哪一步 |
| resume 结论 | 退出码 0，success |

最后一行比 token 计数有力得多：它证明恢复的是**语义上的上下文**，不只是一段计费记录。
`start_new_session` + 统一转发 INT + `stop` + 「前台也能活」这四件事共同依赖的那个单点，
在 0.154.0 上复验通过。

这一轮顺带把 I5 现场复现了：`stop` 刚打印完「上下文保留，可 resume」，
`status` 立刻说 `failed —— 报告缺失或为空＝没正常收尾`、退出码 1，两句话自相矛盾。

**一条不能只住在 spec 里的知识**：实测前台与后台的 Bash 环境**逐字节相同**
（14 个 `CLAUDE_*` 变量、父进程、tty 状态全一样），所以「必须用 `run_in_background`」
**无法编码成硬约束**。这条连同它的缓解措施（信号转发保住上下文）写进代码注释——spec 要删，
知识得有个家。

---

### Task 1: log 解析纯函数（ANSI／运行时 ERROR／session id）

**Files:**
- Create: `codex_agent.py`
- Test: `test_codex_agent.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `strip_ansi(text: str) -> str`
  - `round_separator(kind: str, task: str, when_iso: str) -> str`
  - `current_round(log_text: str) -> str`
  - `runtime_error_lines(log_text: str) -> list[str]`
  - `extract_session_id(log_text: str) -> str | None`
  - 常量 `ROUND_MARK`、`_BENIGN_TARGETS`、`_BENIGN_USER`
  - `Rejected(SystemExit)` / `reject(message)` / 常量 `USAGE_ERROR = 2`
  - `task_name(value) -> str`（argparse 的 `type=`，见 Task 6 的 #3）

**审查后修订（P0-2 / P0-3 / P1-2）：**
- 错误有**三种**锚定形式，不是两种：新增形式 C 顶层致命 `Error:`（大写 E），
  而它正是 spec §9 整节在讲的那个 thread-store conflict。
- 形式 B 按 **module target** 分类，不按自由文本（文本会变，target 不会）。
- **删掉 `TAIL_LINES`**：日志改成追加 + 每轮写分隔符，判据只扫最后一个分隔符之后。
  「末 50 行」那个窗口在偷偷承担「已恢复的错误不算」的语义，而这件事现在由
  target 分类正经做了，窗口成了劣化替代品。
- `--color never` 只给**主线**：2026-09-19 端到端冒烟实测 resume 不认它
  （`error: unexpected argument '--color' found`，整轮当场死掉），codex 也没有
  对应的 config 键（`--strict-config` 探测回 `unknown configuration field color`）。
  所以 resume 那一轮的日志照样带 ANSI，**`strip_ansi` 在 resume 这条路上是承重的**，
  不是可删的防御。测试仍喂带 ANSI 的输入。

- [ ] **Step 1: 写失败的测试**

> 本块由 `/tmp/sync_plan.py` 从 `test_codex_agent.py` 生成，别手工改这里。
> 顶部的 `_no_codex()` / `_full_meta()` / `_HomeSandbox` 是全文件共用的助手。

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'codex_agent'`

- [ ] **Step 3: 最小实现**

> 本块由 `/tmp/sync_plan.py` 从 `codex_agent.py` 生成，别手工改这里。
> `import` 全部在文件头，不在本块里。

```python
# 护栏拒绝走独立退出码。裸 `raise SystemExit("人话")` 的退出码是 1，和
# EXIT["failed"] 撞码——调用方就分不清「--dir 写错了」和「codex 真的失败了」。
# 用 2 是因为它已经是 argparse 的参数错误码：参数写错和被护栏拒绝本来就是一类事。
USAGE_ERROR = 2


class Rejected(SystemExit):
    """护栏拒绝。把「人话」和「退出码」绑在一起，让人不可能只写对一半。"""

    def __init__(self, message):
        self.message = message
        super().__init__(USAGE_ERROR)


def reject(message):
    raise Rejected(message)


# 任务名同时是文件名和 pgrep 的匹配模式，两边都会被奇怪字符咬：
#   `a/b`   写不出文件（裸 FileNotFoundError）
#   `../x`  写到 tasks/ 外面去
#   `a|b`   在 `pgrep -f` 里是**正则的或**，会命中任意含 `b` 的进程——
#           于是 stop 把 SIGINT 发到别人的 codex 上，正是 spec §9 发誓要避免的事
# 放在 argparse 的 type= 上，四个子命令一个都绕不过去。
_TASK_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def task_name(value):
    if not _TASK_NAME.match(value):
        raise argparse.ArgumentTypeError(
            "只允许字母、数字、点、下划线、连字符（任务名既是文件名，也是 pgrep 的匹配模式）")
    return value


USAGE_LIMIT_MARK = "You've hit your usage limit"
THREAD_LOCK_MARK = "already has an active writer"
REPORT_PREVIEW_LINES = 5

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

ROUND_MARK = "===== codex-agent "   # 每轮开跑前写进日志的分隔符前缀

# 本轮被信号打断时留在日志里的痕迹。**刻意不以 ROUND_MARK 开头**：
# current_round 是按 ROUND_MARK 往回切的，这行要是同前缀，它就会被当成新一轮
# 的开始，本轮前面的错误全被丢掉，判据当场失明。
INTERRUPT_MARK = "----- codex-agent 本轮被 INT 打断，上下文保留，可 resume -----"


def round_separator(kind, task, when_iso):
    return f"{ROUND_MARK}{kind} {task} {when_iso} ====="


def note_interrupt(log_path):
    """在信号处理器里往日志追一行打断标记。

    为什么值得为它多写一个函数：把 TERM 转成 INT 保住了上下文，却没人告诉
    下一个读判据的人「这轮是被打断的」。2026-09-19 真机复现过——`stop t` 刚
    打印完「上下文保留，可 resume」，紧接着 `status t` 就说
    `failed —— 报告缺失或为空＝没正常收尾`，退出码 1，两句话自相矛盾。
    前台误跑被 2 分钟超时杀掉时同理：那正是「run_in_background 编不进去」
    那条缓解措施最需要说话的时刻。

    用 O_APPEND + 单次 os.write：小写入在 Linux 上是原子的，不会和 tee 循环
    的缓冲写互相撕裂；也刻意不碰那个已经打开的文件对象——信号处理器随时可能
    插在它的 write 中间。
    写不进去就算了：保住 codex 的上下文优先于留痕。
    """
    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (INTERRUPT_MARK + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass


# codex 自己的错误有三种锚定形式（2026-09-19 对 106 份真实日志全量统计），
# 少认一种就等于判据失效：
#   A 用户层   `ERROR: Reconnecting... 2/5`                  行首是 ERROR:／WARN:   23 行
#   B tracing `<ISO 时间戳> ERROR codex_core::session: …`     行首是时间戳，带 target 92 行
#   C 顶层致命 `Error: thread/resume: … active writer`        行首是大写 Error:       5 行
# 形式 C 首字母是大写 E，`^ERROR:` 大小写敏感、匹配不到它——而它恰恰是「会话被
# 锁死」那条最该报的错。
# 日志里还混着 brief 原文和 codex 转述的子进程输出（cargo 的 error[E0599]、
# pytest 的 `E   KeyError`、markdown 的 `## Warning Signs`），
# 所以绝不能用裸 grep ERROR —— 会大面积误报。
_ERR_USER = re.compile(r"^(?:ERROR|WARN):\s+(.*)")
_ERR_TRACING = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+(?:ERROR|WARN)\s+(\S+?):\s")
_ERR_FATAL = re.compile(r"^Error:\s")          # 形式 C，一律致命，没有白名单

# 形式 B 按 module target 分类，不按自由文本——文本会变，target 不会。
# 白名单之外一律计入判据，**包括 codex_core::session\***（会话建不起来正是最该报的）。
_BENIGN_TARGETS = (
    "codex_models_manager::",                      # 模型列表刷新超时，不影响本次运行
    "codex_api::endpoint::responses_websocket",    # 连接抖动，codex 自己会重连
    "rmcp::transport::worker",
    "codex_core::tools::router",                   # apply_patch 被拒后重打成功
)
# 形式 A 的良性只有这一条，且必须按**前缀**匹配：后缀有 `1/5`~`5/5` 和
# `waiting for network` 多种，写整行字面量会漏掉其余几种。
_BENIGN_USER = ("Reconnecting...",)

_SESSION_ID = re.compile(r"session id:\s*([0-9a-f-]{36})")


def strip_ansi(text):
    """剥掉颜色码。

    主线带了 `--color never`，日志本来就是纯文本；但 **resume 不认 --color、
    codex 也没有对应的 config 键**，resume 那一轮的日志照样带 ANSI。
    所以这个函数在 resume 这条路上是承重的，删不得。
    """
    return _ANSI.sub("", text)


def current_round(log_text):
    """日志是追加的，判据只看最后一个分隔符之后——上一轮的错误不是这一轮的事。"""
    text = strip_ansi(log_text)
    cut = text.rfind(ROUND_MARK)
    return text if cut < 0 else text[cut:]


def runtime_error_lines(log_text):
    """本轮日志里 codex 自己的错误行（已滤掉良性 target 与良性用户层消息）。

    刻意没有「只看末 N 行」的窗口参数。那个窗口过去偷偷承担着「运行中已经恢复
    过去的错误不算」这个语义，而这件事现在由 target 白名单正经做了，窗口只剩下
    劣化替代品的身份：留着它，下一个撞上 60 行尾部堆栈的人就会把 50 改成 500，
    然后每一次已恢复的错误都静默变成 suspect。
    """
    hits = []
    for raw in current_round(log_text).splitlines():
        line = raw.strip()
        if _ERR_FATAL.match(line):
            hits.append(line)
            continue
        m = _ERR_TRACING.match(line)
        if m:
            if not m.group(1).startswith(_BENIGN_TARGETS):
                hits.append(line)
            continue
        m = _ERR_USER.match(line)
        if m and not m.group(1).startswith(_BENIGN_USER):
            hits.append(line)
    return hits


def extract_session_id(log_text):
    m = _SESSION_ID.search(strip_ansi(log_text))
    return m.group(1) if m else None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，8 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: log 解析纯函数——两种形态的运行时 ERROR 判据与 session id 提取"
```

---

### Task 2: 判据函数 `judge()`

**Files:**
- Modify: `codex_agent.py`
- Test: `test_codex_agent.py`

**Interfaces:**
- Consumes: `runtime_error_lines`、`current_round`
- Produces:
  - `Verdict = NamedTuple("Verdict", [("state", str), ("reason", str), ("detail", list)])`
  - `judge(report_path: pathlib.Path, log_path: pathlib.Path, pid) -> Verdict`
  - `clear_report(report_path) -> None`
  - 常量 `USAGE_LIMIT_MARK`、`THREAD_LOCK_MARK`、`REPORT_PREVIEW_LINES = 5`
  - 状态字符串 `"running" / "success" / "suspect" / "failed"`

**审查后修订（P0-1 / P1-1）：**
- **`clear_report`**：codex **只在正常收尾时**写 `-o` 文件、启动时不 truncate。
  run 成功写下报告 → resume 秒死于写锁 → 判据读到**上一轮的旧报告** → 报 success。
  真实日志里有 5 份样本走的正是这条路。run 和 resume 都必须在 spawn 之前删掉它，
  本工具成为报告的唯一创建者，「报告存在」才重新是一句关于本次调用的真话。
- success 的 `detail` 从「JSON 顶层 key」改成「报告前 5 行」：实测 156 份报告
  只有 4 份能解析成 JSON，`-o` 写的是 agent 的最后一条消息，通常是 markdown 散文。
- `thread-store conflict` 和 `usage limit` 一样特判进 `reason`，不新增状态。

- [ ] **Step 1: 写失败的测试**

> 本块由 `/tmp/sync_plan.py` 从 `test_codex_agent.py` 生成，别手工改这里。
> 顶部的 `_no_codex()` / `_full_meta()` / `_HomeSandbox` 是全文件共用的助手。

```python
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
        self.assertLess(len(v.detail), 50)   # 钉住「确实截断了」，不随常量一起动

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

    def test_被INT打断的那轮_reason要告诉人可以resume(self):
        """2026-09-19 真机复现：`stop t` 刚打印完「上下文保留，可 resume」，
        紧接着 `status t` 就说 `failed —— 报告缺失或为空＝没正常收尾`，退出码 1。
        两句话自相矛盾，而调用方拿不到那条唯一有用的信息：这轮是被打断的，
        接着 resume 就行，不用重跑。
        """
        self.log.write_text(ca.INTERRUPT_MARK + "\n")
        v = ca.judge(self.report, self.log, None)
        self.assertEqual(v.state, "failed")     # 产物确实没出来，状态不变
        self.assertIn("resume", v.reason)       # 但处置得说清楚

    def test_打断标记不能被当成新一轮的开始(self):
        # 它要是以 ROUND_MARK 开头，current_round 就会从它这里切开，
        # 本轮前面的错误全被丢掉——判据当场失明
        log = (ca.round_separator("run", "t", "2026-09-19T10:00:00") + "\n"
               + ERR_FATAL + "\n" + ca.INTERRUPT_MARK + "\n")
        self.assertEqual(len(ca.runtime_error_lines(log)), 1)
        self.assertFalse(ca.INTERRUPT_MARK.startswith(ca.ROUND_MARK))

    def test_撞额度上限比被打断更该被说出来(self):
        # 两个都命中时，「换账号」比「可以 resume」更接近真正的处置
        self.log.write_text(ERR_USER_LAYER + "\n" + ca.INTERRUPT_MARK + "\n")
        self.assertIn("额度", ca.judge(self.report, self.log, None).reason)

    def test_非UTF8的日志不许把判据打崩(self):
        # codex 被 INT 打断时可能只写出半截多字节字符
        self.report.write_text("干完了")
        self.log.write_bytes(b"\xff\xfe" + "正常收尾".encode())
        self.assertEqual(ca.judge(self.report, self.log, None).state, "success")

    def test_非UTF8的报告不许把判据打崩(self):
        # codex 被 SIGINT 打断时可能只写出半截字节
        self.report.write_bytes(b"\xff\xfe" + "干完了".encode())
        self.assertEqual(ca.judge(self.report, self.log, None).state, "success")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent -v`
Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'judge'`

- [ ] **Step 3: 最小实现**

> 本块由 `/tmp/sync_plan.py` 从 `codex_agent.py` 生成，别手工改这里。
> `import` 全部在文件头，不在本块里。

```python
def clear_report(report_path):
    """每轮开跑前删掉报告文件。

    2026-09-19 实测：codex **只在正常收尾时**写 `-o` 指定的文件，启动时**不**
    truncate。所以 run 成功写下报告、随后 resume 秒死于写锁时，判据会读到上一轮
    的旧报告并判 success——工具在说谎（真实日志里有 5 份样本走的正是这条路）。
    删掉之后本工具成为报告的唯一创建者，「报告存在」才重新是一句关于本次调用的
    真话。代价是失败的 resume 会连带毁掉上一轮的报告：可以接受，日志是追加的，
    上一轮的内容还在里面。
    """
    report_path.unlink(missing_ok=True)


class Verdict(NamedTuple):
    state: str   # running / success / suspect / failed
    reason: str  # 一行人话
    detail: list # suspect／failed：出事的那几行；success：报告前几行


def judge(report_path, log_path, pid):
    """唯一的成败判据。`run` 收尾和 `status` 共用它，避免两处判据漂移。

    codex 的退出码不可信：中途已恢复的工具 ERROR（apply_patch 被拒后重打成功）
    也会把退出码染成 1。所以判据只看产物和日志，不看退出码。
    """
    if pid is not None:
        return Verdict("running", f"pid={pid} 存活", [])

    log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
    round_text = current_round(log_text)
    errors = runtime_error_lines(log_text)
    # errors="replace"：codex 被 SIGINT 打断时可能只写出半截字节，
    # 裸 read_text 会 UnicodeDecodeError 把判据整个打崩。
    report_text = report_path.read_text(errors="replace") if report_path.exists() else ""

    # “报告没出现＝没正常收尾”——这是 codex 写 -o 的唯一时机。
    # 前提是每轮开跑前把上一轮的报告删掉（见 clear_report），否则旧报告会被
    # 当成本轮的产物，一次失败的运行会被判成 success。
    if not report_text.strip():
        # 两种特判只改 reason、不新增状态：补救手段不同（换账号／新起任务），
        # 但都属于「没正常收尾」这一种事实，状态机不该为此变复杂。
        if USAGE_LIMIT_MARK in round_text:
            return Verdict("failed", "撞上账号额度上限，换账号或等额度恢复", errors)
        if THREAD_LOCK_MARK in round_text:
            return Verdict("failed",
                           "会话被写锁占住（上一轮没真的结束，或曾被 SIGTERM 杀过），只能新起一个任务",
                           errors)
        # 排在上面两条之后：那两条意味着 resume 也救不回来（换账号／新起任务），
        # 而这一条恰恰是「resume 就行」，不能把更坏的消息盖掉。
        if INTERRUPT_MARK in round_text:
            return Verdict("failed", "本轮被 INT 打断，上下文保留——接着 resume 即可，不用重跑", errors)
        return Verdict("failed", "报告缺失或为空＝没正常收尾", errors)

    if errors:
        return Verdict("suspect", f"报告在，但本轮日志有 {len(errors)} 条未分类的 codex 错误", errors)

    # 报告内容由 brief 决定（要 commit 还是要别的），属于任务层不属于工具层。
    # 只预览前几行，让调用方自己核对 brief 要的东西在不在——不解析 JSON：实测
    # 156 份真实报告只有 4 份是 JSON，`-o` 写的是 agent 的最后一条消息，通常是
    # markdown 散文。要结构化输出那是 --output-schema 的事。
    preview = [l for l in report_text.splitlines() if l.strip()][:REPORT_PREVIEW_LINES]
    return Verdict("success", "正常收尾，本轮日志无未分类错误", preview)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，15 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: 判据函数 judge——四态收敛，退出码不参与判断"
```

---

### Task 3: 隔离目录不变量

**Files:**
- Modify: `codex_agent.py`
- Test: `test_codex_agent.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `account_choices() -> list[str]`
  - `isolation_home(account: str) -> pathlib.Path`
  - `auth_source(account: str) -> pathlib.Path`
  - `ensure_isolation(account: str) -> pathlib.Path`（违反不变量时 `raise SystemExit(str)`）
  - `shared_skill_root() -> pathlib.Path`
  - 常量 `CONFIG_NOTE: str`

**审查后修订（P0-4 / P2-2）：**
- **`config.toml` 的内容不是不变量**：实测 codex 自己往里写
  `[projects."…"] trust_level = "trusted"`，`~/.codex-subagent` 已累积 19 段。
  校验内容则第二次 run 就失败，重写则抹掉 codex 的 trust 状态。
  缺失时只创建一份**仅含说明注释**的空配置，唯一的不变量是「它是普通文件，不是软链」。
- `model` / `model_reasoning_effort` / `sandbox_mode` / `approval_policy`
  **一律不写进 config**：CLI 每次都显式传，config 再存一份就是同一条事实两个家，
  还是个会被静默覆盖的缺省值。
- 新增不变量：`~/.agents/skills/` 必须为空。那是 `CODEX_HOME` **管不到**的共享扫描根，
  放了东西 codex 就看得见，隔离的前提直接不成立——**非空即拒跑**，并列出里面有什么。

- [ ] **Step 1: 写失败的测试**

> 本块由 `/tmp/sync_plan.py` 从 `test_codex_agent.py` 生成，别手工改这里。
> 顶部的 `_no_codex()` / `_full_meta()` / `_HomeSandbox` 是全文件共用的助手。

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent -v`
Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'account_choices'`

- [ ] **Step 3: 最小实现**

> 本块由 `/tmp/sync_plan.py` 从 `codex_agent.py` 生成，别手工改这里。
> `import` 全部在文件头，不在本块里。

```python
MODEL = "gpt-6-astra"

# 隔离目录自己的 config，绝不软链主配置。
# 2026 年踩过：`codex-acct` 把 config.toml 软链到主配置，一用就把 MCP、plugins、
# hooks、memories 全带回来，隔离当场失效。账号和隔离是正交的两件事，要组合。
#
# 这份初始内容**刻意只有注释**。model／effort／sandbox_mode／approval_policy
# 由 CLI 每次显式传，写进 config 就是同一条事实有两个家，还是个会被静默覆盖的
# 缺省值（现存两个隔离目录的 model/effort/service_tier 本来就互相打架）。
CONFIG_NOTE = '''# codex-agent 的隔离配置。
# 这个文件必须是本目录自己的普通文件，不许软链 ~/.codex/config.toml——
# 软链会把主配置的 MCP／plugins／hooks／memories 全带回来，隔离当场失效。
# 刻意不写 model / model_reasoning_effort / sandbox_mode / approval_policy：
# 那些由 codex-agent 每次运行显式传参，写在这里只会变成一份会被静默覆盖的缺省值。
# codex 自己会往下面追加 [projects.*] trust_level，那是它的状态，不要手动清。
'''


def account_choices():
    """账号可选项由实际目录扫描得出，不硬编码——加了账号就自动认。"""
    accounts_dir = pathlib.Path.home() / ".codex-accounts"
    extra = sorted(p.name for p in accounts_dir.iterdir() if p.is_dir()) if accounts_dir.is_dir() else []
    return ["default"] + extra


def isolation_home(account):
    base = pathlib.Path.home()
    return base / ".codex-subagent" if account == "default" else base / f".codex-subagent-{account}"


def auth_source(account):
    base = pathlib.Path.home()
    return base / ".codex" / "auth.json" if account == "default" else base / ".codex-accounts" / account / "auth.json"


def shared_skill_root():
    """CODEX_HOME 管不到的共享扫描根。放了东西 codex 就看得见，隔离的前提不成立。"""
    return pathlib.Path.home() / ".agents" / "skills"


def ensure_isolation(account):
    """保证隔离目录满足全部不变量，不满足就拒跑（而不是“尽力而为”地继续）。"""
    d = isolation_home(account)
    # tasks/reports/logs 必须先建好：目录不存在时 codex 不会自己建，`-o` 静默
    # 写失败（log 末尾只留一行 Failed to write last message file），而判据是
    # “报告没出现＝没正常收尾”——一次成功的运行会被判成失败。2026-09-13 连踩两次。
    for sub in ("skills", "plugins", "tasks", "reports", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)

    # 拒跑而不是打印警告：隔离的前提一旦被破坏，本工具的核心承诺就是空的，
    # 而警告会被淹没在几千行 codex 输出里没人看见。
    intruders = sorted(q.name for q in shared_skill_root().iterdir()) if shared_skill_root().is_dir() else []
    if intruders:
        reject(
            f"{shared_skill_root()} 非空：{'、'.join(intruders)}\n"
            f"那是 CODEX_HOME 管不到的共享扫描根，放了东西 codex 就看得见，隔离不成立。清空它再跑。")

    config = d / "config.toml"
    # 唯一的 config 不变量是「它是普通文件」。内容既不校验也不重写：2026-09-19
    # 实测 codex 自己往这个文件里追加 [projects."…"] trust_level = "trusted"，
    # ~/.codex-subagent 已累积 19 段——校验内容则第二次 run 就失败，重写则抹掉
    # codex 自己的 trust 状态。
    if config.is_symlink():
        reject(f"{config} 是软链——隔离会失效（软链主配置会把 MCP/plugins/hooks 全带回来）。\n"
               f"请删掉它，重跑本命令会生成一份新的。")
    if not config.exists():
        config.write_text(CONFIG_NOTE)

    src = auth_source(account)
    if not src.exists():
        reject(f"账号 {account} 没有登录态（{src} 不存在）。先跑 `codex-acct login {account}`。")
    auth = d / "auth.json"
    if not (auth.is_symlink() and auth.resolve() == src.resolve()):
        if auth.exists() or auth.is_symlink():
            auth.unlink()
        auth.symlink_to(src)
    return d
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，21 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: 隔离目录不变量——config.toml 软链即拒跑"
```

---

### Task 4: 命令行组装与 brief 兜底句

**Files:**
- Modify: `codex_agent.py`
- Test: `test_codex_agent.py`

**Interfaces:**
- Consumes: `MODEL`
- Produces:
  - `prepend_skill_guard(brief_text: str) -> str`
  - `build_run_argv(dir_abs: str, effort: str, report_path: str, brief: str) -> list[str]`
  - `build_resume_argv(dir_abs: str, session_id: str, effort: str, report_path: str, brief: str) -> list[str]`
  - `codex_env(home: pathlib.Path) -> dict`
  - 常量 `SKILL_GUARD: str`

- [ ] **Step 1: 写失败的测试**

> 本块由 `/tmp/sync_plan.py` 从 `test_codex_agent.py` 生成，别手工改这里。
> 顶部的 `_no_codex()` / `_full_meta()` / `_HomeSandbox` 是全文件共用的助手。

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent -v`
Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'prepend_skill_guard'`

- [ ] **Step 3: 最小实现**

> 本块由 `/tmp/sync_plan.py` 从 `codex_agent.py` 生成，别手工改这里。
> `import` 全部在文件头，不在本块里。

```python
SKILL_GUARD = "**不得使用任何 skill，除非本 brief 明确指定。**"

# 每次运行都固定带上的参数。调用方碰不到它们，也就不可能漏掉。
# 主线和 resume 都收的参数。调用方碰不到它们，也就不可能漏掉。
# `--color never` **不在这里**：resume 不认它（见 build_resume_argv）。
_COMMON = ["-c", "approval_policy=\"never\"", "-c", "project_doc_max_bytes=0",
           "--skip-git-repo-check", "--disable", "plugins"]


def prepend_skill_guard(brief_text):
    """兜底句前置。CODEX_HOME 隔离是结构性防线，这句是内容层的第二道。"""
    if brief_text.startswith(SKILL_GUARD):
        return brief_text
    return f"{SKILL_GUARD}\n\n{brief_text}"


def build_run_argv(dir_abs, effort, report_path, brief):
    # --cd 必须绝对路径：相对路径启动即崩（log 无 banner + os error 2）。
    # -o 也必须绝对路径：相对路径按「发命令那个 shell 的 cwd」解析、不按 --cd，
    # 实测在 --cd 的 worktree 里怎么找都没有，一度误判成「没正常收尾」。
    # 两条都由调用方传绝对路径进来（cmd_run 里 resolve），这里不做兜底猜测。
    # --color never：实测 --color auto（默认）在输出被重定向时**并不**关颜色，
    # 106 份日志无一例外含 ANSI。主线能从源头关掉，resume 关不掉（见下）。
    return (["codex", "exec", "--cd", dir_abs, "-m", MODEL,
             "-c", f'model_reasoning_effort="{effort}"',
             "--sandbox", "danger-full-access", "--color", "never"] + _COMMON +
            ["-o", report_path, brief])


def build_resume_argv(dir_abs, session_id, effort, report_path, brief):
    # 三处和主线不同，都是实测撞出来的：
    #   1. --cd 必须放在 resume 之前，放后面 clap 直接拒收
    #   2. resume 不认 --sandbox（error: unexpected argument，退出码 2），走 -c sandbox_mode
    #   3. resume 也不认 --color（2026-09-19 端到端冒烟：`error: unexpected argument
    #      '--color' found`，整轮当场死掉）。而 codex 没有对应的 config 键
    #      （--strict-config 探测回 `unknown configuration field \`color\``），
    #      所以 resume 这条路**关不掉颜色**——它的日志会带 ANSI，解析侧的
    #      strip_ansi 在这条路上是承重的，不是防御。
    # resume 收的参数集比主线小一圈，加参数前先 `codex exec resume --help` 对一遍。
    # 另：resume 总用 --cd／当前目录覆盖 workdir，不还原会话原目录，所以 --cd 必带。
    return (["codex", "exec", "--cd", dir_abs, "resume", session_id, "-m", MODEL,
             "-c", f'model_reasoning_effort="{effort}"',
             "-c", 'sandbox_mode="danger-full-access"'] + _COMMON +
            ["-o", report_path, brief])


def codex_env(home):
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    # 会话索引留在主目录，resume 才找得到（隔离的是 skill/plugin，不是会话历史）
    env["CODEX_SQLITE_HOME"] = str(pathlib.Path.home() / ".codex")
    return env
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，26 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: argv 组装——主线与 resume 的参数差异编码进代码"
```

---

### Task 5: 任务元数据与真实 PID 反查

**Files:**
- Modify: `codex_agent.py`
- Test: `test_codex_agent.py`

**Interfaces:**
- Consumes: `isolation_home`、`account_choices`
- Produces:
  - `meta_path(home: pathlib.Path, task: str) -> pathlib.Path`
  - `write_meta(home, task, meta: dict) -> None`
  - `find_meta(task: str) -> (home, meta) | (None, None)`（跨所有隔离目录查）
  - `all_metas() -> list[tuple[pathlib.Path, dict]]`
  - `_load_meta(path) -> dict`（读回来就校验必填键）
  - 常量 `REQUIRED_META_KEYS`

**审查后修订（#9 / #10 / #12）：**
- `_home` 魔法键换成**返回值**。`cmd_resume` 里那句
  `{k: v for k, v in meta.items() if k != "_home"}` 就是「必须记得剥」的证据，
  而「必须记得」正是这个工具存在的理由本身。
- 元数据形状只能有一个定义处：`_load_meta` 读回来就校验 `REQUIRED_META_KEYS`，
  之后所有地方放心裸下标，`.get(键, 默认值)` 一个不留（默认缺省值违反铁律）。
- 元数据**不存 `pid`**：全程零处读取，且存下来的 PID 会过期、会被系统复用。
  - `find_codex_pid(report_path: str) -> int | None`
  - `pid_alive(pid: int) -> bool`

- [ ] **Step 1: 写失败的测试**

> 本块由 `/tmp/sync_plan.py` 从 `test_codex_agent.py` 生成，别手工改这里。
> 顶部的 `_no_codex()` / `_full_meta()` / `_HomeSandbox` 是全文件共用的助手。

```python
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
        """`.` 在 `pgrep -f` 的正则里是「任意字符」。

        任务名 `a` 的报告路径是 `…/reports/a.md`，拿它当模式去匹配，会命中任务
        `aXmd` 的 `…/reports/aXmd.md`（`a` + 任意字符 + `md`）——2026-09-19 实测
        `pgrep -f .../a.md` 确实返回了 aXmd 那个进程的 pid。
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent -v`
Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'meta_path'`

- [ ] **Step 3: 最小实现**

> 本块由 `/tmp/sync_plan.py` 从 `codex_agent.py` 生成，别手工改这里。
> `import` 全部在文件头，不在本块里。

```python
def meta_path(home, task):
    return home / "tasks" / f"{task}.json"


def write_meta(home, task, meta):
    meta_path(home, task).write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def new_meta(task, account, workdir, effort):
    """元数据的**唯一**构造器。字段清单只在这里写一次。

    刻意没有 pid：存活必须每次重新反查，存下来的 PID 会过期、还会被系统复用，
    留着它只会诱导别人犯这个设计本来要防的错。
    """
    return {"task": task, "account": account, "dir": workdir, "effort": effort,
            "session_id": None, "started_at": _now_iso()}


# 校验面由构造器派生，**不另写一份清单**。两份清单必然漂移，而漂移的后果是
# 静默的：少一个字段，run 照常报成败，但那个任务从此 status/resume/stop 全
# 够不着，工具还会建议「删掉它重新 run」——会话就此丢掉。
# 读回来就校验，之后所有地方放心裸下标；`.get(键, 默认值)` 是默认缺省值，
# 正是本工具要消灭的东西。
REQUIRED_META_KEYS = tuple(new_meta("", "", "", "").keys())


def _load_meta(path):
    meta = json.loads(path.read_text())
    missing = [k for k in REQUIRED_META_KEYS if k not in meta]
    if missing:
        reject(f"{path} 缺字段 {missing}，元数据坏了——删掉它重新 run")
    return meta


def find_meta(task):
    """跨所有隔离目录按任务名找，返回 (home, meta)；查不到返回 (None, None)。

    账号是查出来的，不是让调用方再报一遍的。
    home 走返回值，不塞进 meta 里当 `_home` 魔法键：魔法键意味着每个写回元数据
    的地方都得记得把它剥掉，而「必须记得」正是这个工具存在的理由本身。

    查到多份就拒绝，不"取第一个"：那会让 status/resume/stop 静默作用到扫描顺序
    更靠前的那个会话上。而任务名撞车这条路很好走——撞额度上限就该换账号重跑。
    cmd_run 已经不让这个状态建起来，这里是第二道。
    """
    found = []
    for account in account_choices():
        home = isolation_home(account)
        p = meta_path(home, task)
        if p.exists():
            found.append((home, _load_meta(p)))
    if len(found) > 1:
        reject(f"任务名 {task} 在多个隔离目录里都有："
               + "、".join(str(h) for h, _ in found)
               + "\n无法确定该操作哪一个，删掉不要的那份元数据再来。")
    return found[0] if found else (None, None)


def all_metas():
    out = []
    for account in account_choices():
        home = isolation_home(account)
        tasks_dir = home / "tasks"
        if not tasks_dir.is_dir():
            continue
        for p in sorted(tasks_dir.glob("*.json")):
            out.append((home, _load_meta(p)))
    return out


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        # EPERM 是「有这个进程，但不归你管」，不是「已退出」。把它当死，就会
        # 误判「已结束」而去 resume 一个还在跑的会话，撞上它自己的写锁。
        return True
    return True


def find_codex_pid(report_path):
    """存活判定只有一个可靠判据：真实 PID。日志判不了，$! 给不出。

    回合用尽的 codex 留下的 log 和还在跑的长得一模一样（末尾都是正常输出、
    没有收尾标记），所以 tail 日志只能看它在干什么，判不了存活。
    $! 拿到的是包装链最外层（2026-09-17 实测：$! 是 254151，codex 是 254153），
    据此判"已退出"再 resume，会撞上它自己的写锁。

    反查直接扫 /proc，**不用 pgrep**。三条理由，每条都是承重的：

    1. `pgrep -f <模式>` 的模式是**正则**，而报告路径里有 `.`（任务名允许点，
       后缀又是 `.md`）。2026-09-19 实测：任务 `a` 的 `…/reports/a.md` 拿去
       pgrep，命中了任务 `aXmd` 的 `…/reports/aXmd.md`——`a`+任意字符+`md`。
       后果是 `stop a` 把 SIGINT 发给 aXmd 的 codex。转义救不了根：这里要的
       根本不是匹配，是**相等**。报告路径在 codex 的 argv 里正好是独立一项
       （`-o <路径>`），所以按 argv 元素精确比对，正则语义一点都不引入。
    2. comm 必须是 codex：pgrep -f 会命中发命令的 shell 自己（2026-09-19 实测，
       comm=bash），少了这道过滤会把 shell 当成 codex。
    3. 必须限当前用户：本机有别的用户在跑 codex，不限的话 stop 会打到别人身上。

    顺带省掉每次 1+N 次子进程（一个 pgrep 加每个候选一个 ps）。
    """
    me = os.getuid()
    needle = report_path.encode()
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # 进程随时可能退出，每一步都可能 ENOENT——一律跳过，不让它打断扫描
            if entry.stat().st_uid != me:
                continue
            if (entry / "comm").read_bytes().strip() != b"codex":
                continue
            if needle not in (entry / "cmdline").read_bytes().split(b"\0"):
                continue
        except OSError:
            continue
        pid = int(entry.name)
        if pid_alive(pid):
            return pid
    return None


# codex 全集是 minimal/low/medium/high/xhigh/max/ultra，这五档是**刻意裁剪**。
# argparse 的 choices 是唯一守门员——实测 codex 对 `-c model_reasoning_effort=bogus`
# 静默接受、banner 照打 `reasoning effort: bogus_effort_value`，档位写错没人告诉你。
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，32 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: 任务元数据与真实 PID 反查——comm 过滤是承重的"
```

---

### Task 6: CLI 层与四个子命令

**Files:**
- Modify: `codex_agent.py`
- Test: `test_codex_agent.py`

**Interfaces:**
- Consumes: 前五个 Task 的全部产出
- Produces:
  - `build_parser() -> argparse.ArgumentParser`
  - `cmd_run(args) -> int`、`cmd_status(args) -> int`、`cmd_resume(args) -> int`、`cmd_stop(args) -> int`
  - `run_codex(argv: list, env: dict, log_path, home, task, kind) -> None`
    （写本轮分隔符、tee 到屏幕与日志、边跑边抓 session id、把信号统一转成 INT）
  - `main() -> int`
  - 常量 `EXIT = {"success": 0, "failed": 1, "suspect": 3, "running": 4}`

**审查后修订：**
- **P0-1**：`cmd_run` / `cmd_resume` 都在 spawn 之前 `clear_report(report)`。
- **P0-3**：日志改**追加**（`"ab"`），`run_codex` 进来先写一行本轮分隔符。
  `run_codex` 因此多收一个 `kind`（`run`／`resume`）——分隔符由它自己写，
  调用方不可能忘。
- **P1-1**：报告路径 `reports/<任务>.md`；PID 反查的字符串跟着变。
- **P2-1**：元数据**不存 `pid`**。存活必须每次重新反查，存一个会过期、还会被
  系统复用的 PID，只会诱导别人犯这个设计本来要防的错。
- **P2-3**：`EFFORTS` 旁注明五档是对 codex 全集
  （`minimal/low/medium/high/xhigh/max/ultra`）的刻意裁剪，且 argparse 的
  `choices` 是唯一守门员——实测 codex 对 `-c model_reasoning_effort=bogus`
  **静默接受**、banner 照打 `reasoning effort: bogus_effort_value`。

**实现时发现、计划原稿没有的三处（工作子代理补）：**
- `proc.stdout.read(1024)` 会**阻塞到攒够 1024 字节**，tee 就不是实时的，
  session id 也要等攒够才写进元数据（`status` 在这段窗口里查不到它）。
  改用 `read1(1024)`：有多少给多少。
- `EXIT.get(state, 0)` 是**默认缺省值**，而 `running` 恰好落进这个默认里——
  `codex-agent status t && 下一步` 会把「还在跑」当成功。改成四个状态都在表里、
  用 `EXIT[state]` 直接取，缺哪个就 KeyError 当场炸，不静默给 0。
- **同一个任务名出现在两个隔离目录**里时，`find_meta` 只返回先扫到的那个，
  `status`／`resume`／`stop` 会静默作用到错的会话上。而这条路很好走：撞额度上限
  → 换账号重跑同名任务。所以 `find_meta` 查到多份就**拒绝并列出**，
  `cmd_run` 发现任务名已属于别的账号也**拒绝**——让这个状态压根建不起来。

- [ ] **Step 1: 写失败的测试**

> 本块由 `/tmp/sync_plan.py` 从 `test_codex_agent.py` 生成，别手工改这里。
> 顶部的 `_no_codex()` / `_full_meta()` / `_HomeSandbox` 是全文件共用的助手。

```python
class TestTaskName(unittest.TestCase):
    def test_收正常任务名(self):
        for good in ("smoke-2026-09-19", "a.b_c", "T1"):
            with self.subTest(good=good):
                self.assertEqual(ca.task_name(good), good)

    def test_拒会咬到文件系统和pgrep的字符(self):
        # `../x` 写到 tasks/ 外面、`a/b` 写不出文件、`a|b` 在 pgrep -f 里是正则或，
        # 会命中无关进程 —— 于是 stop 把 SIGINT 发到别人的 codex 上
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

    def test_任务名校验挂在四个子命令上_结构上绕不过(self):
        parser = ca.build_parser()
        for argv in (["run", "--task", "a|b", "--dir", "/tmp", "--brief", "b.md",
                      "--effort", "low", "--account", "default"],
                     ["status", "a|b"], ["resume", "a|b", "--brief", "b.md", "--effort", "low"],
                     ["stop", "a|b"]):
            with self.subTest(cmd=argv[0]), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_resume和stop不收account_账号是查出来的(self):
        parser = ca.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["resume", "t", "--brief", "b.md", "--effort", "low",
                               "--account", "default"])

    def test_不提供会造成误用的参数(self):
        parser = ca.build_parser()
        # 和 spec §5「不提供的参数」那张表一字不差
        for bad in ["--timeout", "--background", "-o", "--log", "--model", "--sandbox"]:
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parser.parse_args(["run", "--task", "t", "--dir", "/tmp", "--brief", "b.md",
                                   "--effort", "low", "--account", "default", bad, "x"])


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

    def test_resume也要校验隔离不变量_spec要求每次run和resume都查(self):
        # spec §6 写的是「每次 run/resume 都校验」。resume 这条路上不查的话，
        # config.toml 被软链回主配置、auth.json 指错账号，全都查不出来。
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
        self.assertEqual(ca.EXIT, {"success": 0, "failed": 1, "suspect": 3, "running": 4})
        self.assertEqual(ca.USAGE_ERROR, 2)
        # 护栏拒绝必须和四个判据结论都区分得开
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
        with mock.patch.object(ca.subprocess, "Popen") as popen, \
             mock.patch.object(ca.signal, "signal") as sigsig:
            self._run_once(popen)
            handled = {c.args[0] for c in sigsig.call_args_list}
            self.assertEqual(handled, {signal.SIGTERM, signal.SIGINT, signal.SIGHUP})
            sigsig.call_args_list[0].args[1](signal.SIGTERM, None)
        popen.return_value.send_signal.assert_called_with(signal.SIGINT)


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
    """用真进程、真信号验证 spec §9 的那条保证：

    无论谁怎么停包装器，codex 收到的永远**只有** INT。
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest test_codex_agent -v`
Expected: FAIL，`AttributeError: module 'codex_agent' has no attribute 'build_parser'`

- [ ] **Step 3: 最小实现**

> 本块由 `/tmp/sync_plan.py` 从 `codex_agent.py` 生成，别手工改这里。
> `import` 全部在文件头，不在本块里。

```python
# 四个状态，按「最该放行 → 最该拦住调用方」排序，退出码和严重度都从这**一份**
# 派生——两份清单必然漂移。
# 严重度不能直接拿退出码比：suspect 的码(3)比 failed(1)大，按码取 max 会让一个
# 真失败被一个 suspect 盖过去。
# 退出码取值一律 EXIT[state]，不写 .get(state, 默认值)：有默认值的话 running 会
# 悄悄落成 0，`codex-agent status t && deploy` 就会在任务还在跑的时候部署。
# 2 不在表里，留给参数错误与护栏拒绝（见 USAGE_ERROR）。
_STATES = (("success", 0), ("running", 4), ("suspect", 3), ("failed", 1))
EXIT = dict(_STATES)
_SEVERITY = [name for name, _ in _STATES]


def _worse(a, b):
    return a if _SEVERITY.index(a) >= _SEVERITY.index(b) else b


# session id 在 banner 里，前几百字节就出现。攒到这个上限还没有就不再攒，
# 免得几 MB 的输出全堆在内存里。
_HEAD_LIMIT = 8192


def _report_path(home, task):
    # .md 不是 .json：`-o` 写的是 agent 的最后一条消息，实测 156 份里只有 4 份
    # 能解析成 JSON，其余都是 markdown 散文。后缀名要说真话。
    return home / "reports" / f"{task}.md"


def _log_path(home, task):
    return home / "logs" / f"{task}.log"


def run_codex(kind, home, task, meta, make_argv):
    """唯一的 spawn 入口。开跑前必须做的三件事全在这里，调用方不需要记住顺序：
    ① 元数据落盘 ② 删掉上一轮的报告 ③ 日志追加一行本轮分隔符。

    **报告路径由本函数拥有**，回传给 `make_argv` 去拼命令；`env` 也由 `home`
    派生。原签名收现成的 `argv` 和 `env`，自己却又重新推导一遍报告路径去删，
    于是留下三条没人保证的「必须记得对齐」：argv 里 `-o` 指的那个文件、被删掉
    的那个文件、元数据文件名指的那个任务，得是同一个。三条一起错时它一声不吭
    ——clear_report 删了别的文件（防陈旧报告这条 P0 静默失效）、日志分隔符说谎、
    元数据内容和文件名对不上。现在这三条在结构上就违反不了了。

    这三件事原先还散在调用方，实测漏掉「先 write_meta」会在 codex **已经跑起来
    之后**才炸 FileNotFoundError，子进程当场变孤儿。

    （stdin／timeout／start_new_session 各自的理由写在它们那一行旁边。）
    """
    report = _report_path(home, task)
    argv = make_argv(str(report))
    env = codex_env(home)

    # 开跑前的三件事，全部在 spawn **之前**做完：任何一件炸了，codex 都还没起来，
    # 不会留下一个没人管的孤儿进程。
    write_meta(home, task, meta)
    clear_report(report)

    # 日志追加不覆盖，先写一行本轮分隔符——判据只扫它之后的内容。
    # 分隔符由本函数自己写，调用方不可能忘；忘了判据就会把上一轮的错误算到这一轮头上。
    with open(_log_path(home, task), "ab") as log:
        log.write((round_separator(kind, task, _now_iso()) + "\n").encode())
        log.flush()

        # start_new_session=True 不是为了 detach，是为了挡**组信号**。2026-09-19 实测：
        # codex 与包装器同进程组时，一发 `kill -TERM -<组>`（harness 停掉后台 Bash 任务
        # 就是这么干的）会直接把 codex TERM 死，而 SIGTERM 之后 thread 永久锁死、
        # 再也 resume 不了、上下文全丢。隔到独立会话后 codex 收不到任何组信号，
        # 只会收到下面 handler 转发的 INT。
        #
        # stdin 固定接 /dev/null：否则 codex 等 stdin 永久挂死（日志只剩
        # "Reading additional input from stdin" + 进程 0% CPU）。
        # 不设 timeout：会误杀正当的长任务。
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)

        def forward_as_sigint(signum, frame):
            # 无论包装器被谁、用什么信号停，codex 收到的永远是 INT，上下文永远可 resume。
            # 刻意不在这里退出：让 tee 循环自然跑完，判据照样出、完成通知照样带结论。
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            # 顺手留痕，让之后跑判据的人（包括另一个进程里的 status）知道
            # 这轮是被打断的，处置是 resume 而不是重跑。
            note_interrupt(_log_path(home, task))

        # 转发只在 codex 活着的这段时间里生效，出去时原样还回去——改全局信号处置
        # 而不还原，等于把本函数的副作用留给了整个进程的余生。
        previous = {sig: signal.signal(sig, forward_as_sigint)
                    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        try:
            _tee_until_exit(proc, log, home, task, meta)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def _tee_until_exit(proc, log, home, task, meta):
    head, session_id = b"", None
    # read1：有数据就返回，不等凑满。用 read 会阻塞到满 1024 字节或 EOF——
    # codex 的 banner 只有 ~170 字节，之后可能思考几十分钟，这期间屏幕、日志、
    # 元数据里的 session id 全是空的（实测父进程 4.06 秒才看到 t=0 就 flush 的
    # 172 字节）；包装进程此时被杀，这一轮就再也 resume 不回来。
    for chunk in iter(lambda: proc.stdout.read1(1024), b""):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        log.write(chunk)
        log.flush()
        if session_id is None and len(head) < _HEAD_LIMIT:
            head += chunk
            session_id = extract_session_id(head.decode("utf-8", "replace"))
            if session_id:
                meta["session_id"] = session_id
                write_meta(home, task, meta)
    proc.wait()


def _print_verdict(task, verdict):
    print(f"\n[codex-agent] {task}: {verdict.state} —— {verdict.reason}")
    for line in verdict.detail:
        print(f"  {line}")


def cmd_run(args):
    workdir = pathlib.Path(args.dir).expanduser().resolve()
    if not workdir.is_dir():
        reject(f"--dir {args.dir} 不是目录")
    brief_file = pathlib.Path(args.brief).expanduser()
    if not brief_file.is_file():
        reject(f"--brief {args.brief} 不是文件（brief 只收文件路径，避开引号地狱）")

    home = isolation_home(args.account)
    old_home, old_meta = find_meta(args.task)
    if old_meta is not None:
        # 换账号重跑同名任务很好走（撞额度上限时就该这么干），但那会让同一个名字
        # 出现在两个隔离目录里：find_meta 按账号顺序查，另一份就成了再也够不着的
        # 孤儿元数据，而「上一轮会被覆盖」那句提示在跨账号时还是假话（报告路径不同）。
        if old_home != home:
            reject(f"任务名 {args.task} 已经属于账号 {old_meta['account']}（{old_home}）。\n"
                   f"同名任务跨账号会让 status/resume/stop 指向哪个变得不确定，换个任务名。")
        if find_codex_pid(str(_report_path(home, args.task))) is not None:
            reject(f"任务名 {args.task} 还在跑，换个名字或先 `codex-agent stop {args.task}`")
        print(f"[codex-agent] 提示：任务名 {args.task} 复用，上一轮的报告会被删掉、日志会被追加")

    ensure_isolation(args.account)
    report = _report_path(home, args.task)
    brief = prepend_skill_guard(brief_file.read_text())
    print(f"[codex-agent] 已在 brief 前自动加上：{SKILL_GUARD}")

    run_codex("run", home, args.task,
              new_meta(args.task, args.account, str(workdir), args.effort),
              lambda r: build_run_argv(str(workdir), args.effort, r, brief))

    verdict = judge(report, _log_path(home, args.task), None)
    _print_verdict(args.task, verdict)
    print(f"  报告 {report}\n  日志 {_log_path(home, args.task)}")
    return EXIT[verdict.state]


def cmd_status(args):
    if args.task:
        home, meta = find_meta(args.task)
        if meta is None:
            reject(f"没有这个任务：{args.task}")
        rows = [(home, meta)]
    else:
        rows = all_metas()
    if not rows:
        print("还没有任何任务")
        return EXIT["success"]
    worst = "success"
    for home, meta in rows:
        report, log = _report_path(home, meta["task"]), _log_path(home, meta["task"])
        verdict = judge(report, log, find_codex_pid(str(report)))
        print(f"{meta['task']:<24} {meta['account']:<8} {verdict.state:<8} "
              f"{verdict.reason}  {meta['dir']}")
        for line in verdict.detail:
            print(f"    {line}")
        worst = _worse(worst, verdict.state)
    return EXIT[worst]


def cmd_resume(args):
    home, meta = find_meta(args.task)
    if meta is None:
        reject(f"没有这个任务：{args.task}")
    report = _report_path(home, args.task)
    # resume 之前必须确认真的退出了：对还在跑的会话 resume，报的错和 SIGTERM 锁死
    # 一模一样（thread-store conflict），而处置完全相反——一个该等，一个该弃。
    if find_codex_pid(str(report)) is not None:
        reject(f"任务 {args.task} 还在跑，resume 会撞上它自己的写锁。等它结束，或先 stop。")
    if not meta["session_id"]:
        reject(f"任务 {args.task} 没有记到 session id，无法 resume，只能新起一个任务")
    workdir = pathlib.Path(meta["dir"])
    if not workdir.is_dir():
        reject(f"任务 {args.task} 的工作目录 {workdir} 不在了（worktree 被删？）。"
               f"codex 会以 os error 2 当场崩，所以这里直接拒。")
    brief_file = pathlib.Path(args.brief).expanduser()
    if not brief_file.is_file():
        reject(f"--brief {args.brief} 不是文件")

    ensure_isolation(meta["account"])
    brief = prepend_skill_guard(brief_file.read_text())
    # 元数据描述的是**最后一次调用**：effort 和开跑时间都刷新。
    # 完整的轮次历史不在这里，在日志的分隔符里（每轮一行，带时间戳）。
    meta["effort"] = args.effort
    meta["started_at"] = _now_iso()
    run_codex("resume", home, args.task, meta,
              lambda r: build_resume_argv(meta["dir"], meta["session_id"], args.effort, r, brief))
    verdict = judge(report, _log_path(home, args.task), None)
    _print_verdict(args.task, verdict)
    return EXIT[verdict.state]


def cmd_stop(args):
    home, meta = find_meta(args.task)
    if meta is None:
        reject(f"没有这个任务：{args.task}")
    pid = find_codex_pid(str(_report_path(home, args.task)))
    if pid is None:
        print(f"任务 {args.task} 已经不在跑了")
        return EXIT["success"]
    # 只发 SIGINT。SIGTERM 会让 thread 永久锁死，之后 resume 永远报
    # thread-store conflict，等多久都不释放，上下文全丢。
    os.kill(pid, signal.SIGINT)
    print(f"已向 {args.task} (pid={pid}) 发 SIGINT，上下文保留，可 resume")
    return EXIT["success"]


def build_parser():
    p = argparse.ArgumentParser(
        prog="codex-agent",
        description="把执行类任务派给 codex 后台跑。用 Bash(run_in_background: true) 启动 run。")
    sub = p.add_subparsers(dest="cmd", required=True)

    # 五个参数全必填：不设默认值，因为隐式选中的账号／难度是最容易被误用的地方
    r = sub.add_parser("run", help="起一个新任务")
    r.add_argument("--task", required=True, type=task_name,
                   help="任务名，全局唯一（PID 反查和产物命名都靠它）")
    r.add_argument("--dir", required=True, help="codex 的工作目录，自动转绝对路径")
    r.add_argument("--brief", required=True, help="brief 文件路径（只收文件，不收内联字符串）")
    r.add_argument("--effort", required=True, choices=EFFORTS, help="难度分档")
    r.add_argument("--account", required=True, choices=account_choices(), help="codex 账号")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="看任务状态；省略任务名则列出全部")
    s.add_argument("task", nargs="?", type=task_name)
    s.set_defaults(func=cmd_status)

    # resume/stop 不收 --account：账号从元数据查出来，不可能指错
    m = sub.add_parser("resume", help="给已结束的任务补一轮")
    m.add_argument("task", type=task_name)
    m.add_argument("--brief", required=True)
    m.add_argument("--effort", required=True, choices=EFFORTS)
    m.set_defaults(func=cmd_resume)

    k = sub.add_parser("stop", help="停一个任务（只发 SIGINT）")
    k.add_argument("task", type=task_name)
    k.set_defaults(func=cmd_stop)
    return p


def main():
    """入口。

    **「必须用 `Bash(run_in_background: true)` 启动」这一条编不进硬约束**，
    2026-09-19 实测过为什么：前台和后台两种方式下，本进程看到的环境**逐字节相同**
    ——14 个 `CLAUDE_*` 变量、父进程、tty 状态全一样。没有任何信号能让本进程判断
    自己是不是跑在 harness 的追踪之下，所以这条只能留在 SKILL.md 当软约定。

    能做的是把它的**灾难性后果**消掉，那已经做了：前台跑被 2 分钟超时杀掉时，
    包装器把收到的信号统一转成 INT 再转发（见 run_codex），codex 的上下文保住、
    仍可 resume，日志里还留下一行打断标记（见 note_interrupt）告诉下一个人该
    resume 而不是重跑。于是误用的代价从「会话永久锁死、上下文全丢」降到
    「这一轮没拿到完成通知」——可恢复，且判据会把话说清楚。
    """
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except Rejected as e:
        print(e.message, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest test_codex_agent -v`
Expected: PASS，全部测试绿

- [ ] **Step 5: 加执行权限并自检**

```bash
chmod +x codex_agent.py
python3 codex_agent.py --help
python3 codex_agent.py run --help
```
Expected: 帮助正常打印，`--account` 的 choices 里能看到 `default acct2 acct3`

**不在本分支建 `~/.local/bin/codex-agent` 软链**（#16/#17）：软链必须指向改名后的
最终路径，指向工作树的话工作树一删命令就断。软链、目录改名、合并都由编排者在合并
后做。Task 8 的冒烟直接用 `python3 <绝对路径>/codex_agent.py` 调，验的是同一份代码。

- [ ] **Step 6: 提交**

```bash
git add codex_agent.py test_codex_agent.py
git commit -m "feat: CLI 四个子命令——参数强制显式，resume/stop 的账号从元数据查出来"
```

---

### Task 7: 改写 SKILL.md

**Files:**
- Modify: `SKILL.md`（250 行 → 约 40 行）

**Interfaces:**
- Consumes: `codex_agent.py` 的 CLI 契约
- Produces: 新 SKILL.md

**不做的事（#16/#17）：** 本分支**不建软链、不改目录名、不合并**。
软链必须指向改名后的最终路径，指向工作树的话工作树一删命令就断；
这三件由编排者在合并后一起做。

- [ ] **Step 1: 自检 CLI 可用**

```bash
python3 "$PWD/codex_agent.py" --help
python3 "$PWD/codex_agent.py" run --help
```
Expected: 帮助正常打印，`--account` 的 choices 里能看到 `default acct2 acct3`

- [ ] **Step 2: 改写 SKILL.md**

新 SKILL.md 只保留**人／模型才能决定的事**，全文约 40 行，包含且仅包含：

1. frontmatter：`name: codex-agent`，description 说明"派执行类任务给 codex 后台跑，
   省 claude token；用 Bash run_in_background 启动"。
2. 一句话定位：把**有明确规划的执行类任务**派给 codex 干，claude 只编排。
3. 启动方式：

   ````markdown
   ## 启动：一个任务 = 一次 `Bash(run_in_background: true)`

   ```bash
   codex-agent run --task <任务名> --dir /abs/repo --brief brief.md --effort low --account default
   ```

   五个参数**全必填**，没有默认值。`run_in_background: true` 是唯一正确的启动方式——
   harness 追踪它、面板可监控、**完成时的通知里直接带成败结论**。
   绝不 `nohup … &`：detach 之后就只剩存活、没有通知。
   ````
4. effort 分档表（从旧 SKILL.md 原样搬，这是判断力，不是约束）。
5. 另外三条命令各一行：

   ```bash
   codex-agent status [任务名]        # 省略则列出全部；running/success/suspect/failed
   codex-agent resume <任务名> --brief follow.md --effort low
   codex-agent stop <任务名>          # 只发 SIGINT，上下文保留
   ```
6. 三件调用方仍需要知道的事：
   - 退出码：`0` success、`1` failed、`3` suspect（干完了但本轮日志有未分类的 codex
     错误，要人看一眼）、`4` running（`status` 才会出现）、`2` 参数写错或被护栏拒绝。
   - brief 只收**文件路径**；工具会自动前置"不得使用任何 skill"兜底句。
   - 要传 skill 给 codex：在 brief 里写该 skill 的**绝对路径**让它自己读，不要动共享目录。
7. 一句收尾：codex 不靠谱就换 claude 子代理。

**不写进 SKILL.md 的**（已经是代码保证的，写了就是重复）：`--cd` 绝对路径、`</dev/null`、
`-o` 路径、`mkdir -p`、不加 `&`／timeout、PID 怎么反查、`kill -INT`、
exit 1 ≠ 失败、resume 的 flag 差异、隔离目录怎么建、`--color never`、任务名字符集。

- [ ] **Step 3: 核对行数与内容**

```bash
wc -l SKILL.md
grep -cE '\-\-cd|/dev/null|pgrep|kill -INT|mkdir -p|--color' SKILL.md
```
Expected: 行数 ≤ 50；第二条命中数为 0（这些约束已在代码里，文档不该再说一遍）

- [ ] **Step 4: 提交**

```bash
git add SKILL.md
git commit -m "docs: SKILL.md 收敛到 40 行——约束进了代码，文档只留判断力"
```

---

### Task 8: 端到端冒烟

**Files:**
- 无新文件（验证性任务）

**Interfaces:**
- Consumes: 全部

**必须用 `Bash(run_in_background: true)` 跑**：前台 2 分钟超时会杀掉包装器，
而组信号一旦落到 codex 身上会话就永久锁死——这正是本工具要防的那件事。
调用方式用 `python3 <绝对路径>/codex_agent.py`（本分支不建软链，见 Task 7）。

- [ ] **Step 1: 造一个最小的真任务**

```bash
mkdir -p /tmp/codex-agent-smoke
cat > /tmp/codex-agent-smoke/brief.md <<'EOF'
在当前目录创建文件 hello.txt，内容为一行：codex-agent smoke ok
然后用一两句话说明你干了什么收尾。
EOF
```

- [ ] **Step 2: 跑起来（必须用 Bash run_in_background: true）**

```bash
python3 <仓库绝对路径>/codex_agent.py run --task smoke-2026-09-19 \
  --dir /tmp/codex-agent-smoke --brief /tmp/codex-agent-smoke/brief.md \
  --effort low --account default
```
Expected: 屏幕有 codex 的实时输出；结束时打印
`smoke-2026-09-19: success —— 正常收尾，本轮日志无未分类错误`，退出码 0

- [ ] **Step 3: 验证产物与判据**

```bash
cat /tmp/codex-agent-smoke/hello.txt
python3 <仓库绝对路径>/codex_agent.py status smoke-2026-09-19
cat ~/.codex-subagent/tasks/smoke-2026-09-19.json
cat ~/.codex-subagent/reports/smoke-2026-09-19.md
head -3 ~/.codex-subagent/logs/smoke-2026-09-19.log
```
Expected: 文件内容正确；status 报 success；元数据里 `session_id` 非 null、无 `pid` 字段；
日志第一行是 `===== codex-agent run smoke-2026-09-19 <时间> =====`

- [ ] **Step 4: 验证 resume**

```bash
echo "再创建 hello2.txt，内容 second round，然后一两句话收尾。" > /tmp/codex-agent-smoke/follow.md
python3 <仓库绝对路径>/codex_agent.py resume smoke-2026-09-19 \
  --brief /tmp/codex-agent-smoke/follow.md --effort low
cat /tmp/codex-agent-smoke/hello2.txt
grep -c '^===== codex-agent' ~/.codex-subagent/logs/smoke-2026-09-19.log
```
Expected: resume 成功，第二个文件出现（证明 `--cd` 与 session 都正确恢复）；
日志里有 2 行分隔符（证明日志是追加的）

- [ ] **Step 5: 验证护栏真的拦得住**

```bash
A="python3 <仓库绝对路径>/codex_agent.py"
$A run --task smoke-2026-09-19 --dir /不存在 --brief /tmp/codex-agent-smoke/brief.md --effort low --account default; echo "退出码 $?"
$A run --task x --dir /tmp --brief /tmp/codex-agent-smoke/brief.md --effort 中等 --account default; echo "退出码 $?"
$A run --task 'a|b' --dir /tmp --brief /tmp/codex-agent-smoke/brief.md --effort low --account default; echo "退出码 $?"
$A resume 不存在的任务 --brief /tmp/codex-agent-smoke/brief.md --effort low; echo "退出码 $?"
```
Expected: 四条全部被拒绝、给出人话错误信息，退出码都是 2（和 codex 真失败的 1 区分得开）

- [ ] **Step 6: 清理并提交冒烟结论**

```bash
rm -rf /tmp/codex-agent-smoke
git commit --allow-empty -m "test: 端到端冒烟通过——run/status/resume/护栏四项验证"
```
