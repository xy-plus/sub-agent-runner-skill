# codex-sub-agent 账号自动分配 Implementation Plan (v3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `--account auto` 让工具自己挑账号，撞上额度上限就记下恢复时间并换下一个。

**Architecture:** 核心是一条约束——**换账号由一次独立探测决定，不由任务日志决定**。任务日志永远可能被 brief 原文、codex 读文件的回显、子进程输出污染（今天已真实发生）；探测用写死在代码里、不含任何额度字样的 prompt，它的输出不可能被回显污染。日志只决定「要不要花 15 秒探一下」——一个廉价闸门，误判的代价只是白探一次。

**Tech Stack:** Python 3 标准库。无第三方依赖，不许新增。

配套 spec：`docs/superpowers/specs/2026-09-22-codex-auto-account-design.md`（v3）。
**计划里任何一处说不清楚的地方，去读 spec——那里写了每个决定的理由。**

## 已完成（分支 auto-account，勿重做）

| Task | commit | 内容 |
|---|---|---|
| 1 | `168ab59` | 额度判据：常量缩短成 `"hit your usage limit"`，`hit_usage_limit(error_lines)` 建在已分类错误行上 |
| 2 | `ce4ebed` | `parse_reset_time` |
| 3 | `d3964ac` | `usage_limit.json` 读写 |
| 4 | `557e000` | 保留名护栏 + `accounts_by_availability` |
| — | `df25237` `ad75c64` | 审查修正：删无效测试、读写校验收紧 |

基线：**280 条测试全绿**。`parse_reset_time` / `write_usage_limit` / `accounts_by_availability`
**目前还没有生产调用方**——本计划就是来接上它们的。

## Global Constraints

- 探测的 prompt 必须是**模块级常量**且不含任何额度字样。这是它全部可信度的来源，做成参数就能被调用方污染
- 探测用 `--sandbox read-only`：它只是去问一句话，不该有动任何东西的能力
- 探测走 `subprocess.run`；测试 patch `ca.subprocess.run` 绕过它。`setUpModule` 里那条
  「argv[0] != codex」的断言是最后一道兜底，**不许动它**
- 恢复时间**只从探测的输出解析**，绝不从任务日志解析
- 排序**一次都不读时钟**。唯一读 `now` 的地方是「只有时刻」那种消息的换算，发生在**写入时**，存绝对时间
- 不新增状态码，不给 `Verdict` 加字段
- 需要 HOME 沙箱的测试类一律继承 `_HomeSandbox`；patch 写 `mock.patch`；临时目录不加 rmtree
- 不设测试数量门槛
- 提交时**不要传** `-c user.email` / `-c user.name`，也不要设 `GIT_AUTHOR_*` / `GIT_COMMITTER_*`

---

### Task 5: `judge` 里打断标记优先于额度字样

**Files:** `codex_sub_agent.py`（`judge` 里三条特判的顺序）、`test_codex_sub_agent.py`

**为什么（实现者必读）：** 打断标记是**本工具自己写的**（`interrupt_codex` 在发完 INT 之后写），
是关于「**我们**做了什么」的不可伪造证据。额度字样是可伪造的文本。两者同时出现时，
打断必须赢——而且「两者都真」几乎不可能：codex 撞上限会自己退出，那时根本没有进程可以被 INT。

现有的 `test_撞额度上限比被打断更该被说出来` 钉的是**相反**的优先级。那条是在额度判据还只是
「给人看的解释」时写的；现在它要决定**删不删元数据、换不换账号**，优先级的分量完全不同。

- [ ] **Step 1: 写失败的测试**

```python
    def test_打断标记优先于额度字样(self):
        # 打断标记是本工具自己写的，是关于「我们做了什么」的不可伪造证据；
        # 额度字样可以来自 brief 回显、codex 读文件的回显、子进程输出。
        # 两者同时出现，打断必须赢——否则一轮「被我们打断、上下文还在、resume 就行」
        # 的任务，会被判成额度问题，进而（在 cmd_run 里）删元数据、换账号重跑。
        v = self._judge(ERR_USER_LAYER_REAL + "\n" + ca.INTERRUPT_MARK + "\n")
        self.assertEqual(v.state, "interrupted")
        self.assertIn("resume", v.reason)
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
cd /home/xy/.claude/skills/codex-sub-agent/.worktree/auto-account
python3 -m unittest test_codex_sub_agent -k 打断标记优先 -v
```
预期：FAIL，拿到的是 `failed` + 额度。

- [ ] **Step 3: 把 `has_interrupt_mark` 那一支提到最前**

`judge()` 里，在 `if not report_text.strip():` 之后，把三条特判重排成：
打断 → 额度 → 写锁 → 兜底。并把原来写在额度那一支上方的注释改写成：

```python
        # **打断排最前。** 这个标记是本工具自己写的（interrupt_codex 发完 INT 才写），
        # 是关于「我们做了什么」的**不可伪造**证据；下面两条靠的是日志里的字面串，
        # 而日志里混着 brief 原文、codex 读文件的回显、子进程输出——2026-09-22 实测：
        # 一个完全正常的账号，因为任务内容涉及额度处理，日志里就出现了
        # `ERROR: …hit your usage limit`。
        # 「两者都真」几乎不可能：codex 撞上限会自己退出，那时没有进程可以被 INT。
        # 顺序反了的代价是不对称的：把「被打断、resume 就行」判成额度问题，
        # cmd_run 会删掉元数据、换账号把整个任务重跑一遍。
        if has_interrupt_mark(round_text):
            return Verdict("interrupted",
                           "本轮被 INT 打断，上下文保留——接着 resume 即可，不用重跑", errors)
```

- [ ] **Step 4: 改掉那条钉反了的既有测试**

`test_撞额度上限比被打断更该被说出来` 整条替换成：

```python
    def test_没有打断标记时_额度字样要点名(self):
        # 原来这条叫「撞额度上限比被打断更该被说出来」，钉的是相反的优先级。
        # 那是额度判据还只是「给人看的解释」时写的；现在它要决定删不删元数据、
        # 换不换账号，而打断标记是不可伪造的那一个。见 test_打断标记优先于额度字样。
        v = self._judge(ERR_USER_LAYER_REAL + "\n")
        self.assertEqual(v.state, "failed")
        self.assertIn("额度", v.reason)
```

- [ ] **Step 5: 跑全量确认通过**

```bash
python3 -m unittest test_codex_sub_agent 2>&1 | tail -3
```

- [ ] **Step 6: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "fix: judge 里打断标记优先于额度字样

打断标记是本工具自己写的，是关于「我们做了什么」的不可伪造证据；
额度字样来自日志，而日志里混着 brief 原文和 codex 读文件的回显。
两者都真几乎不可能：codex 撞上限会自己退出，那时没有进程可以被 INT。

顺序反了的代价不对称：把「被打断、resume 就行」判成额度问题之后，
cmd_run 会删元数据、换账号把整个任务重跑一遍。

原来那条钉反了的测试是在额度判据还只是「解释」时写的，一并改掉。"
```

---

### Task 6: 认出「只有时刻」那种恢复时间

**Files:** `codex_sub_agent.py`（`parse_reset_time`）、`test_codex_sub_agent.py`

**为什么（全量实测，不是设想）：** 现网 268 份日志里，真·额度错误有两种形态：

| 形态 | 行 / 文件 | 谁 |
|---|---|---|
| `try again at Sep 25th, 2026 5:04 PM` | 32 / 16 | Pro 账号，周额度 |
| `try again at 11:10 AM`（**只有时刻**） | 22 / 11 | 带 `Upgrade to Pro` 字样，Plus 账号，5 小时档 |

**41% 的真实额度消息现在解析不出时间**，退化成「无记录＝排最前＝下次照样先试它」，
于是 5 小时窗口里每次 run 都要白撞一次 + 白探 15 秒。

歧义（11:10 是今天还是明天）只在**读到这条消息的那一刻**存在，而消息说的是 "try again **at**"
——未来。所以：读一次 `now`，取**大于等于 now 的下一个 11:10**，存成绝对朴素时间。
**时钟只在这里读一次，排序仍然一次都不读。**

- [ ] **Step 1: 写失败的测试**

```python
    def test_只有时刻没有日期的那种_换算成下一个该时刻(self):
        # 现网 268 份日志里 11 份是这种（Plus 账号的 5 小时档）。
        # 消息说的是 "try again AT"——未来，所以取 >= now 的下一个该时刻。
        # 时钟只在这里读一次；排序一次都不读（见 accounts_by_availability）。
        now = datetime.datetime(2026, 9, 22, 9, 0)
        got = ca.parse_reset_time("or try again at 11:10 AM.", now=now)
        self.assertEqual(got[0], datetime.datetime(2026, 9, 22, 11, 10), "同一天的晚些时候")
        self.assertEqual(got[1], "11:10 AM")

    def test_只有时刻_已经过了就算明天(self):
        now = datetime.datetime(2026, 9, 22, 15, 0)
        got = ca.parse_reset_time("or try again at 11:10 AM.", now=now)
        self.assertEqual(got[0], datetime.datetime(2026, 9, 23, 11, 10))

    def test_只有时刻_正好等于now就算今天(self):
        # 边界：消息说 at 11:10，此刻正好 11:10，那就是现在，不是明天
        now = datetime.datetime(2026, 9, 22, 11, 10)
        self.assertEqual(ca.parse_reset_time("try again at 11:10 AM.", now=now)[0], now)

    def test_带日期的那种不受now影响(self):
        # 带日期的消息自带全部信息，不许被 now 改写
        for now in (datetime.datetime(2020, 1, 1), datetime.datetime(2030, 1, 1)):
            self.assertEqual(
                ca.parse_reset_time("try again at Sep 25th, 2026 5:04 PM", now=now)[0],
                datetime.datetime(2026, 9, 25, 17, 4))

    def test_两种形态都要用现网真实字节验一次(self):
        # fixture 经过我的手，真实字节没有。见模块头清单第 8 条。
        seen = set()
        for f in glob.glob('/home/xy/.codex-subagent*/logs/*.log'):
            for line in ca.runtime_error_lines(pathlib.Path(f).read_text(errors="replace")):
                if ca.USAGE_LIMIT_MARK not in line:
                    continue
                got = ca.parse_reset_time(line, now=datetime.datetime(2026, 9, 22, 9, 0))
                if got is not None:
                    seen.add("带日期" if got[1][0].isalpha() else "只有时刻")
        self.assertEqual(seen, {"带日期", "只有时刻"},
                         "现网两种形态都必须解析得出；只覆盖一种说明判据还有一半是瞎的")
```

测试文件顶部若还没 `import glob`，加上。

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m unittest test_codex_sub_agent -k 时刻 -v
python3 -m unittest test_codex_sub_agent -k 真实字节 -v
```

- [ ] **Step 3: 实现**

`parse_reset_time` 改成收一个**必填**的 `now`（不给默认值——仓库规范第 6 条：
不要缺省值。调用方必须显式交出它用的是哪个时钟）：

```python
def parse_reset_time(text, now):
    """从撞上限的那段话里抠出恢复时间，抠不到就 `None`。

    现网两种形态（2026-09-22 对 268 份日志全量核对）：

        try again at Sep 25th, 2026 5:04 PM     32 行 / 16 份   Pro，周额度
        try again at 11:10 AM                   22 行 / 11 份   Plus，5 小时档

    第二种**只有时刻没有日期**，占真实消息的 41%。不认它的代价是：
    5 小时窗口里每次 run 都要白撞这个账号一次、白探 15 秒。

    歧义（11:10 是今天还是明天）只在读到消息的那一刻存在，而消息说的是
    "try again **at**"——未来。所以取 **>= now 的下一个该时刻**。
    `now` **必填**：这是本功能唯一读时钟的地方，调用方必须显式交出它用的是哪个，
    否则测试里一个隐式的 `datetime.now()` 会让「明天还是今天」随测试运行的时刻飘。
    排序（`accounts_by_availability`）**一次都不读时钟**。

    返回 `(时间, 原始串)`——两样来自同一次匹配：原始串是落盘时的审计线索，
    拆成两个函数就可能各匹配各的、对不上。返回的是**朴素本地时间**，不编造时区
    （消息里就没有）。安全性来自它只当排序键。
    """
```

正则加一条「只有时刻」的分支（用具名分组，别用位置号——加一个括号就整体漂移）：

```python
_RESET_AT_DATED = re.compile(
    r"try again at\s+(?P<raw>"
    r"(?P<month>[A-Z][a-z]{2})\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th),\s+"
    r"(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<half>[AP]M))")
_RESET_AT_TIME_ONLY = re.compile(
    r"try again at\s+(?P<raw>(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<half>[AP]M))")
```

实现（先试带日期的，再试只有时刻的；**小时必须在 1–12**，别取模——
`99:04 PM` 取模成 15:04 是在静默编造恢复时间）：

```python
def _to_24h(hour, half):
    """12 小时制换 24 小时制；小时不在 1–12 就返回 `None`。

    **不取模。** `int("99") % 12 + 12 == 15`——一条畸形消息会被静默编造成
    一个看起来合理的恢复时间，而这个值要落盘、要排序、要显示给人看。
    """
    hour = int(hour)
    if not 1 <= hour <= 12:
        return None
    return hour % 12 + (12 if half == "PM" else 0)
```

`parse_reset_time` 主体：

```python
    m = _RESET_AT_DATED.search(text)
    if m is not None:
        month = _MONTHS.get(m.group("month"))
        hour = _to_24h(m.group("hour"), m.group("half"))
        if month is None or hour is None:
            return None
        try:
            return (datetime.datetime(int(m.group("year")), month, int(m.group("day")),
                                      hour, int(m.group("minute"))), m.group("raw"))
        except ValueError:            # Feb 31st 这种
            return None
    m = _RESET_AT_TIME_ONLY.search(text)
    if m is None:
        return None
    hour = _to_24h(m.group("hour"), m.group("half"))
    if hour is None:
        return None
    when = now.replace(hour=hour, minute=int(m.group("minute")), second=0, microsecond=0)
    if when < now:
        when += datetime.timedelta(days=1)
    return when, m.group("raw")
```

现有调用点和测试里所有 `parse_reset_time(...)` 都要补 `now=` 实参。

- [ ] **Step 4: 跑全量确认通过**

```bash
python3 -m unittest test_codex_sub_agent 2>&1 | tail -3
```

- [ ] **Step 5: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "feat: 认出「只有时刻没有日期」那种恢复时间

现网 268 份日志全量核对：真·额度消息有两种形态，带日期的 32 行/16 份
（Pro，周额度），只有时刻的 22 行/11 份（Plus，5 小时档）。后者占 41%，
原来一律解析不出，于是 5 小时窗口里每次 run 都白撞一次、白探 15 秒。

歧义只在读到消息那一刻存在，消息说的是 try again AT——未来，
所以取 >= now 的下一个该时刻。now 必填、不给缺省：这是本功能唯一读时钟
的地方，隐式的 datetime.now() 会让「今天还是明天」随测试运行时刻飘。
排序一次都不读时钟。

顺手修掉小时取模：99:04 PM 原来会被静默编造成 15:04。"
```

---

### Task 7: 独立探测

**Files:** `codex_sub_agent.py`（`parse_reset_time` 下面）、`test_codex_sub_agent.py`（新建 `TestProbeAccountQuota`）

**Interfaces:**
- Consumes: `codex_env`、`runtime_error_lines`、`hit_usage_limit`、`parse_reset_time`、`strip_ansi`
- Produces: `PROBE_PROMPT`、`PROBE_TIMEOUT`、`probe_account_quota(home, now) -> (bool, tuple|None)`

**为什么（实测支撑）：** 2026-09-22 拿一个真·限流账号实跑：**退出码 1（和普通失败没区别）、
15 秒、消息在 stderr，而 prompt 回显也在 stderr**——没有可用的非文本信号，分流也救不了。
所以证据只能是文本，而任务日志的文本是被污染的。**唯一的出路是换一份没被污染的文本**：
用我们自己写死的 prompt 单独问一次。

- [ ] **Step 1: 写失败的测试**

```python
class TestProbeAccountQuota(_HomeSandbox):
    """探测是「换不换账号」的唯一依据，所以它自己的可信度就是整条链路的上界。"""

    OUT_OF_QUOTA = ("OpenAI Codex v0.155.1\n--------\nsession id: 01a0\n--------\n"
                    "user\n" + "ok" + "\n"
                    "ERROR: You’ve hit your usage limit. Visit https://chatgpt.com/codex/"
                    "settings/usage to purchase more credits or try again at "
                    "Sep 26th, 2026 5:47 PM.\n")
    FINE = ("OpenAI Codex v0.155.1\n--------\nsession id: 01a0\n--------\nuser\nok\n"
            "thinking\ncodex\nok\n")

    def setUp(self):
        super().setUp()
        self.home = ca.ensure_isolation("default")
        self.now = datetime.datetime(2026, 9, 22, 9, 0)

    def _probe(self, stderr_text, returncode=1):
        done = subprocess.CompletedProcess(args=[], returncode=returncode,
                                           stdout="", stderr=stderr_text)
        with mock.patch.object(ca.subprocess, "run", return_value=done) as run:
            got = ca.probe_account_quota(self.home, now=self.now)
        return got, run

    def test_探测用的prompt是常量且不含额度字样(self):
        # **这是探测全部可信度的来源。** prompt 里只要有「usage limit」几个字，
        # codex 的回显就会把它写进探测自己的输出，探测就开始自证自己撞了上限。
        # 做成常量而不是参数——参数就能被调用方污染，那就绕回原点了。
        self.assertNotIn("usage limit", ca.PROBE_PROMPT.lower())
        self.assertIsInstance(ca.PROBE_PROMPT, str)

    def test_撞上限_返回True和恢复时间(self):
        (limited, hit), _ = self._probe(self.OUT_OF_QUOTA)
        self.assertTrue(limited)
        self.assertEqual(hit[0], datetime.datetime(2026, 9, 26, 17, 47))

    def test_没撞上限_返回False(self):
        (limited, hit), _ = self._probe(self.FINE, returncode=0)
        self.assertFalse(limited)
        self.assertIsNone(hit)

    def test_撞上限但时间解析不出_仍然是True(self):
        # 「撞上了」和「几点恢复」是两件事，**不许用后者的缺失推翻前者**。
        # 推翻的话，一个消息格式变了的账号会被永远当成可用的反复重试。
        (limited, hit), _ = self._probe(
            "ERROR: You’ve hit your usage limit. 后面什么都没有\n")
        self.assertTrue(limited)
        self.assertIsNone(hit)

    def test_探测本身失败_一律当成没撞上限(self):
        # 探不出来就不换号：宁可少试一个账号（调用方重跑即可），
        # 也不要凭猜去删元数据、重跑任务。
        for boom in (OSError("boom"), subprocess.TimeoutExpired("codex", 1)):
            with self.subTest(故障=type(boom).__name__):
                with mock.patch.object(ca.subprocess, "run", side_effect=boom):
                    self.assertEqual(ca.probe_account_quota(self.home, now=self.now),
                                     (False, None))

    def test_探测是只读沙箱且跳过git检查(self):
        # 探测只是去问一句话，不该有动任何东西的能力。
        _, run = self._probe(self.OUT_OF_QUOTA)
        argv = run.call_args[0][0]
        self.assertIn("read-only", argv)
        self.assertIn("--skip-git-repo-check", argv)
        self.assertNotIn("-o", argv, "探测不产出报告，给了 -o 就会去写别人的报告文件")

    def test_探测走这个账号的隔离目录(self):
        _, run = self._probe(self.OUT_OF_QUOTA)
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], str(self.home))

    def test_探测不读stdin(self):
        # 实测：不关 stdin 时 codex 停在「Reading additional input from stdin...」不动，
        # 探测会挂满整个超时。
        _, run = self._probe(self.OUT_OF_QUOTA)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_stdout和stderr都要看(self):
        # 实测额度消息在 stderr，但那是当前版本的行为，不是契约。
        done = subprocess.CompletedProcess(args=[], returncode=1,
                                           stdout=self.OUT_OF_QUOTA, stderr="")
        with mock.patch.object(ca.subprocess, "run", return_value=done):
            limited, _ = ca.probe_account_quota(self.home, now=self.now)
        self.assertTrue(limited)
```

测试文件顶部已有 `subprocess`，不用加。

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m unittest test_codex_sub_agent.TestProbeAccountQuota -v
```

- [ ] **Step 3: 实现**

```python
# 探测用的 prompt。**写死在代码里，且不含任何额度字样。**
# 这是探测全部可信度的来源：codex 会把 prompt 原样回显进输出（实测在 stderr），
# prompt 里只要有「usage limit」几个字，探测就开始自证自己撞了上限。
# 做成常量而不是参数——参数就能被调用方污染，那就绕回原点了。
PROBE_PROMPT = "reply with two letters: ok"
# 实测一个真·限流账号 15 秒返回。给足余量，但必须有上界：
# 探测卡住不能把整条 run 拖死。
PROBE_TIMEOUT = 120


def probe_account_quota(home, now):
    """单独问一次这个账号还有没有额度。返回 `(撞上限了吗, (恢复时间, 原始串) 或 None)`。

    **「换不换账号」由本函数决定，不由任务日志决定。** 任务日志里混着 brief 原文、
    codex 读文件的回显、子进程输出——2026-09-22 实测：一个**完全正常**的账号，
    因为任务内容涉及额度处理，日志里就出现了 `ERROR: …hit your usage limit`
    （codex 带行号 cat 出了含这个字面串的文件）。`runtime_error_lines()` 只认格式
    不认来源，挡不住它。而在 `cmd_run` 里，误判的后果是**删掉元数据、换账号把整个
    任务重跑一遍**。

    探测的输出只含我们自己写死的 prompt，**不可能有被回显进来的假证据**。

    返回**两个值而不是一个**：「撞上了」和「几点恢复」是两件事，现网 41% 的额度消息
    只有时刻甚至没有时间（见 `parse_reset_time`）。合成一个返回值的话，
    「时间解析不出」会被当成「没撞上限」，于是一个消息格式变了的账号会被永远
    当成可用的反复重试。

    **探测本身失败（起不来、超时）一律当成没撞上限**：宁可少试一个账号
    （调用方重跑即可），也不要凭猜去删元数据、重跑任务。

    代价：账号真的满了，15 秒、零 token（服务端直接拒）；账号其实没满，
    会真跑一轮极短的 prompt。只在「本轮已失败且日志里有额度字样」时才发生。
    """
    argv = ["codex", "exec", "--cd", str(home), "-m", MODEL,
            "--sandbox", "read-only", "--color", "never",
            "--skip-git-repo-check", PROBE_PROMPT]
    try:
        done = subprocess.run(argv, env=codex_env(home), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, errors="replace",
                              timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False, None
    # 两个流都看：实测额度消息在 stderr，但那是当前版本的行为，不是契约。
    text = strip_ansi(done.stdout + done.stderr)
    if not hit_usage_limit(runtime_error_lines(text)):
        return False, None
    return True, parse_reset_time(text, now=now)
```

`MODEL` 和 `_COMMON` 已在文件里。**不要**复用 `build_run_argv`：它带 `-o`、带 effort、
带 `danger-full-access`，每一条对探测都是错的。

- [ ] **Step 4: 跑全量 + 拿真实字节验一次**

```bash
python3 -m unittest test_codex_sub_agent 2>&1 | tail -3
python3 -c "
import codex_sub_agent as ca, glob, pathlib, datetime
now = datetime.datetime(2026, 9, 22, 9, 0)
ok = 0
for f in glob.glob('/home/xy/.codex-subagent*/logs/*.log'):
    t = pathlib.Path(f).read_text(errors='replace')
    lines = ca.runtime_error_lines(t)
    if ca.hit_usage_limit(lines): ok += 1
print('真实日志判出额度上限的文件数:', ok)
"
```

- [ ] **Step 5: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "feat: 独立探测——换账号由它决定，不由任务日志决定

任务日志里混着 brief 原文、codex 读文件的回显、子进程输出。2026-09-22 实测：
一个完全正常的账号，因为任务内容涉及额度处理，日志里就出现了额度错误行。
runtime_error_lines() 只认格式不认来源，挡不住它；而误判的后果是删元数据、
换账号重跑整个任务。

实测过没有非文本信号可用：撞上限的退出码是 1（和普通失败没区别），
消息在 stderr，而 prompt 回显也在 stderr。所以只能换一份没被污染的文本——
用写死在代码里、不含额度字样的 prompt 单独问一次。

返回两个值：「撞上了」和「几点恢复」是两件事，现网 41% 的消息只有时刻。
合成一个的话，时间解析不出会被当成没撞上限。
探测本身失败一律当成没撞上限：宁可少试一个账号，也不要凭猜删状态重跑。"
```

---

### Task 8: `--account auto` 与重试循环

**Files:** `codex_sub_agent.py`（`cmd_run` 重写、`build_parser` 的 `--account`）、`test_codex_sub_agent.py`（新建 `TestRunRetry`）

**Interfaces:** Consumes 前面全部产物。Produces 无新公开函数。

**实现者必读的四条：**

1. `run_codex` 在 spawn **之前**就把元数据写进该账号的 home。不删旧的，第二次尝试会被
   `cmd_run` 自己的跨账号同名护栏挡住。
2. **最后一个候选失败时不删。** 删了的话全满之后任务凭空消失——`status` 列表为空、
   `status <任务名>` 说「没有这个任务」。
3. **「原账号一定排第一，所以 auto 模式不需要跨账号护栏」有反例。** 登录态过滤会把该账号
   整个剔出候选（codex 的 access_token 只活十天，这条路很好走），于是第一轮就写到别的 home，
   旧的那份还在，`find_meta` 当场数出两份，`status`/`stop` 从此全退 2。所以迁移要**显式做**。
4. **入口那道「还在跑」只查一次，它不替代 `run_codex` 内部每次 spawn 前的等待**——
   后者问的是「上一轮的 writer 和 codex 排干了没有」，每轮都必须做。本 Task 不碰 `run_codex`。

- [ ] **Step 1: 写失败的测试**

测试**不替换 `run_codex`**，用仓库既有的 `_no_codex()` 路子 mock `Popen`，
让真实的 `cmd_run → run_codex → judge` 全程跑起来。参数从**真实 parser** 取。

```python
class TestRunRetry(_HomeSandbox):
    """mock 的是 `Popen`，不是 `run_codex`——清报告、写分隔符、轮次边界、等上一轮
    都住在 `run_codex` 里，把它整个换掉，要验的行为就一起没了（理由见 `_no_codex`）。
    """

    LIMIT_LINE = ("ERROR: You’ve hit your usage limit. Visit https://chatgpt.com/codex/"
                  "settings/usage to purchase more credits or try again at "
                  "Sep 25th, 2026 5:04 PM.\n")

    def setUp(self):
        super().setUp()
        for name in ("acct2", "acct3"):
            d = self.home / ".codex-accounts" / name
            d.mkdir(parents=True)
            (d / "auth.json").write_text("{}")
        self.workdir = self.home / "repo"
        self.workdir.mkdir()
        self.brief = self.home / "b.md"
        self.brief.write_text("干活")
        self.probed = []

    def _args(self, account, task="t"):
        return ca.build_parser().parse_args(
            ["run", "--task", task, "--dir", str(self.workdir), "--brief", str(self.brief),
             "--effort", "low", "--account", account, "--no-skill"])

    def _probe_says(self, out_of_quota):
        """out_of_quota: 账号名集合。探测对集合里的账号说「满了」。"""
        def fake(home, now):
            account = "default" if home.name == ".codex-subagent" else \
                      home.name.replace(".codex-subagent-", "")
            self.probed.append(account)
            if account not in out_of_quota:
                return False, None
            return True, (datetime.datetime(2026, 9, 25, 17, 4), "Sep 25th, 2026 5:04 PM")
        return mock.patch.object(ca, "probe_account_quota", fake)

    def _rounds(self, popen, texts):
        """让第 i 次 spawn 的 stdout 吐出 texts[i]，之后 EOF。"""
        它 = iter(texts)
        def one(*a, **k):
            proc = mock.MagicMock()
            proc.stdout.read1.side_effect = [next(它).encode(), b""]
            proc.wait.return_value = 1
            return proc
        popen.side_effect = one

    def test_探测说满了才换号(self):
        with _no_codex() as popen, self._probe_says({"acct2"}):
            self._rounds(popen, [self.LIMIT_LINE, "一切正常\n"])
            # 第二轮要产出报告，否则也算失败
            ...
```

> **给实现者：** 上面最后那条只写到一半，是**刻意的**——让 mock 的第二轮"成功"需要在
> `Popen` 的 side_effect 里顺手把报告文件写出来（`run_codex` 会先把它删掉）。
> 请照着 `test_开跑前三件事全在spawn之前做完` 的 `snapshot` 写法自己补完：在 side_effect
> 里拿到当前账号的 `_report_path` 并写入内容。**补完之后把这行注释删掉。**

要覆盖的行为，每条一个测试（名字自拟，但断言必须钉住括号里那件事）：

| 场景 | 必须断言 |
|---|---|
| 第一个满、第二个成 | 尝试顺序；退出码 0；**每轮发出去的 brief/effort/skills 逐字相同**（skills 用**非空**集合，空集合测不出参数有没有被漏传） |
| **满、满、成功** | 退出码 **0**，reason 不含「全部」——v2 的 `len(exhausted) > 1` 会把这个报成失败 |
| 全部满 | 退出码 1；reason 含「全部」；**`cmd_status` 仍列得出这个任务**（调 `cmd_status`，不是只调 `find_meta`）；每个账号和它的恢复时间成对出现 |
| 日志有额度字样但**探测说正常** | **不换号、不删元数据、不写 `usage_limit.json`**；退出码按原判据；只探了一次 |
| brief 含额度字样 + 真实打断标记 | 退出 **130**；**一次探测都没发生**（`self.probed == []`）；元数据还在 |
| 非额度失败 | 立即停止，只试一个账号，退出码是该失败本身的码 |
| 强制模式 `--account acct2` 撞上限 | 只启动一次、只探一次、记录了恢复时间、退出码 1 |
| 每个候选最多启动一次 | `len(spawn 次数) == len(set(...)) == len(候选)` |
| 原账号掉了登录态 | 显式迁移；结束后 **`all_metas()` 只有一份** |
| 所有账号都没登录态 | `reject`，且**每个账号名都出现在错误信息里** |
| 写 `usage_limit.json` 抛 `OSError` | **重试照常继续**，断言第二个账号确实被启动 |

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m unittest test_codex_sub_agent.TestRunRetry -v
```

- [ ] **Step 3: 改 parser**

```python
    r.add_argument("--account", required=True, choices=account_choices() + [AUTO],
                   help=f"codex 账号；{AUTO} = 自动挑一个没在限流的，撞上额度上限就换下一个")
```

- [ ] **Step 4: 重写 `cmd_run`**

`--dir` / `--brief` 那两段校验**一行不改**。从 `home = isolation_home(args.account)` 起换成：

```python
    old_home, old_meta = find_meta(args.task)
    if old_meta is not None:
        # 只查一次，进循环之前：它问的是「这个任务名此刻有没有活着的 codex」，
        # 与试哪个账号无关。**不替代 run_codex 内部每次 spawn 前的等待。**
        if find_codex_pid(_report_path(old_home, args.task)) is not None:
            reject(f"任务名 {args.task} 还在跑，换个名字或先 `codex-sub-agent stop {args.task}`")
        print(f"[codex-sub-agent] 提示：任务名 {args.task} 复用，上一轮的报告会被删掉、日志会被追加")

    if args.account == AUTO:
        candidates = accounts_by_availability(args.task)
        if not candidates:
            reject("没有任何账号有登录态，auto 模式无从分配：\n"
                   + "\n".join(f"  {a} 缺 {auth_source(a)}" for a in account_choices())
                   + "\n先跑 `codex-acct login <账号>`。")
        # **迁移要显式做，不能靠「原账号一定排第一」暗中保证。** 那句话有反例：
        # 登录态过滤会把该账号整个剔出候选（access_token 只活十天），于是第一轮就
        # 写到别的 home，旧的那份还在，find_meta 当场数出两份，status/stop 从此全退 2。
        if old_home is not None and old_home not in [isolation_home(a) for a in candidates]:
            print(f"[codex-sub-agent] 任务原本在账号 {old_meta['account']}，"
                  f"但它现在没有登录态，迁移到 {candidates[0]}")
            meta_path(old_home, args.task).unlink(missing_ok=True)
    else:
        if old_meta is not None and old_home != isolation_home(args.account):
            reject(f"任务名 {args.task} 已经属于账号 {old_meta['account']}（{old_home}）。\n"
                   f"同名任务跨账号会让 status/resume/stop 指向哪个变得不确定，换个任务名。")
        candidates = [args.account]

    brief = prepend_skill_guard(brief_file.read_text(), args.skills)
    exhausted = []          # [(账号, 恢复时间 or None)]
    for attempt, account in enumerate(candidates):
        home = ensure_isolation(account)
        round = run_codex("run", home, args.task,
                          new_meta(args.task, account, str(workdir), args.effort, args.skills),
                          lambda r: build_run_argv(str(workdir), args.effort, r, brief))
        verdict = judge(round)
        limited = False
        # 日志只决定「要不要花 15 秒探一下」——一个廉价闸门。**换不换账号由探测决定。**
        if verdict.state == "failed" and hit_usage_limit(runtime_error_lines(round.text)):
            limited, hit = probe_account_quota(home, now=datetime.datetime.now())
            if limited:
                if hit is not None:
                    write_usage_limit(home, *hit)   # 写失败它自己吞掉并出声
                exhausted.append((account, hit[0] if hit else None))
                if args.account == AUTO and attempt + 1 < len(candidates):
                    # 最后一个候选不走这里：删了的话全满之后任务凭空消失。
                    meta_path(home, args.task).unlink(missing_ok=True)
                    when = f"，恢复于 {hit[0]:%m-%d %H:%M}" if hit else ""
                    print(f"[codex-sub-agent] {account} 撞上额度上限{when}，换下一个账号")
                    continue
        # **两个条件缺一不可。** v2 写成 `len(exhausted) > 1` 且放在 limited 之外，
        # 实测把「满、满、成功」报成了「全部 2 个账号都撞上额度上限」并退出 1。
        if limited and len(exhausted) == len(candidates):
            verdict = Verdict("failed", f"全部 {len(exhausted)} 个账号都撞上额度上限",
                              [f"{a:<8} " + (f"恢复于 {t:%m-%d %H:%M}" if t else "恢复时间未知")
                               for a, t in exhausted])
        _print_verdict(args.task, verdict)
        print(f"  报告 {_report_path(home, args.task)}\n  日志 {_log_path(home, args.task)}")
        return EXIT[verdict.state]
```

`ensure_isolation(account)` 返回的就是 `home`（末行 `return d`）。

- [ ] **Step 5: 跑全量**

```bash
python3 -m unittest test_codex_sub_agent 2>&1 | tail -3
```

- [ ] **Step 6: 提交**（信息里写清楚「换号由探测决定」和「汇总条件两个缺一不可」）

---

### Task 9: 文档同步

**Files:** `SKILL.md`、`README.md`

- [ ] **Step 1: `SKILL.md` 账号那一行**

```
| 账号 | `--account auto` —— **默认就写这个**，工具自己挑一个没在限流的，撞上额度上限会自动换下一个重试，全部满了才报错并告诉你每个账号什么时候恢复。**一个账号可以同时开多个子代理**，不用排队等前一个跑完。要盯某一个账号就写它的名字（`default` / `acct2` / `acct3`，由 `~/.codex-accounts/` 扫出来，加一个账号就自动认），那样撞上限**不会**换号。每个账号有各自独立的隔离目录，互不干扰 |
```

- [ ] **Step 2: `README.md`**

```bash
grep -n "account\|七种" README.md
```
- 示例里的 `--account default` 改成 `--account auto`
- 参数说明加一行：`| --account | auto 自动挑（撞额度上限自动换号重试），或写死某个账号名 |`
- **「七种空测试」的说法要跟着改**——清单现在是 8 条

- [ ] **Step 3: 核对文档和 CLI 一致**

```bash
./codex_sub_agent.py run --help | grep -A2 -- --account
grep -n "auto" SKILL.md README.md
```

- [ ] **Step 4: 提交**

---

## Self-Review 结果

**Spec v3 验收判据覆盖**：

| spec 判据 | Task |
|---|---|
| 1 额度字样识别（直/弯引号 + 真实字节） | 已完成（`168ab59`） |
| 2 污染不许造成换号 | Task 7 + Task 8（「探测说正常」那条） |
| 3 打断优先 | **Task 5** + Task 8（`self.probed == []`） |
| 4 探测输入写死 | Task 7 第一条测试 |
| 5 恢复时间（含小时 1–12、raw 不含前缀） | 已完成（`ce4ebed`）+ **Task 6 补「只有时刻」那种** |
| 6 缓存失效模式 + 写盘失败不阻断 | 已完成（`d3964ac` `ad75c64`）+ Task 8 的 `OSError` 那条 |
| 7 分配与重试全部 11 条 | Task 8 |
| 8 保留名 | 已完成（`557e000`） |
| 9 既有测试全绿 | 每个 Task 的 Step「跑全量」 |
| 10 收尾 | Task 9 + 工作流程第 8 步 |

**新增（spec v3 写完之后才发现的，已写进 Task 6）：** 现网额度消息有**两种**形态，
「只有时刻没有日期」占 41%，原来一律解析不出。spec 只写了带日期那一种。

**占位符扫描：** 无 TBD/TODO。Task 8 Step 1 有一处**刻意留白**（mock 的第二轮怎么产出报告），
已标注为什么留白、照哪条既有测试补、补完要删注释。
