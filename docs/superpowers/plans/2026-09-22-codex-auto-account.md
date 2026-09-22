# codex-sub-agent 账号自动分配 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `--account auto` 让工具自己挑账号，撞上额度上限就记下恢复时间并换下一个重试。

**Architecture:** 三层，自下而上互不依赖。① 一个对引号免疫的撞上限谓词；② 恢复时间的解析与落盘（每个隔离目录一个 `usage_limit.json`）；③ `cmd_run` 里按恢复时间排序的重试循环。核心约束：**状态只排顺序，不准拒绝**——「全部账号都满了」永远来自真实尝试。

**Tech Stack:** Python 3 标准库（`re` / `json` / `datetime` / `pathlib`），`unittest`。无第三方依赖，且不许新增。

## Global Constraints

- `USAGE_LIMIT_MARK` 的值必须是 `"hit your usage limit"`——**不含任何标点字符**
- 状态文件名 `usage_limit.json`，放隔离目录根部，**键只有 `reset_at` 和 `raw` 两个**
- `reset_at` 存**朴素本地时间**的 ISO 串（`datetime.isoformat()`，不带时区）
- 排序键是 `(这个任务是否已住在该账号, reset_at, 账号名)`，无记录用 `datetime.min`
- `--account` **仍然必填**，`choices` = `account_choices()` + `["auto"]`
- 尝试次数上界 = 账号数，**不许有循环**
- 全部撞上限仍是 `failed`，**不新增状态码**（沿用 judge 既有规矩：特判只改 reason）
- 测试数**只增不减**（当前 250）
- 提交时**不要传** `-c user.email` / `-c user.name`，也不要设 `GIT_AUTHOR_*` / `GIT_COMMITTER_*`
- 每个新函数都要有中文 docstring 说明**为什么这么做**，不只是做了什么（沿用本文件既有风格）

---

## File Structure

| 文件 | 职责 | 为什么不拆 |
|---|---|---|
| `codex_sub_agent.py` | 全部实现 | 这是个单文件 CLI，既有模式就是单文件。新增约 90 行，拆出去会让「一个可执行文件即全部」这个性质消失 |
| `test_codex_sub_agent.py` | 全部测试 | 同上 |
| `SKILL.md` / `README.md` | 文档同步 | 两份面向不同读者（skill 激活时 / GitHub 首页），都要改 |

新增的四个函数按依赖顺序排：`hit_usage_limit` → `parse_reset_time` → `read_usage_limit` / `write_usage_limit` → `accounts_by_availability` → `cmd_run` 用它们。

---

### Task 1: 修掉 `USAGE_LIMIT_MARK` 永不匹配

**Files:**
- Modify: `codex_sub_agent.py:140`（常量）、`judge()` 里用到它的那一行
- Test: `test_codex_sub_agent.py`（`ERR_USER_LAYER` 附近 + judge 的测试类）

**Interfaces:**
- Consumes: 无
- Produces: `hit_usage_limit(round_text) -> bool`，`USAGE_LIMIT_MARK = "hit your usage limit"`

**背景（实现者必读）：** 现有常量是 `"You've hit your usage limit"`，用 ASCII 直引号 U+0027；codex 实际输出的是弯引号 U+2019 `You’ve`。所以这个常量**从来没匹配过**，`judge()` 里那条特判是死代码，`status` 把撞上限的任务报成「报告缺失或为空」。测试没发现，是因为 fixture（第 107 行 `ERR_USER_LAYER`）注释写着「真实日志片段」，但抄的时候字节被规范化成了直引号——fixture 和常量用同一个错字符，测试永远绿。

- [ ] **Step 1: 先把这种空测试记进模块头的清单**

打开 `test_codex_sub_agent.py` 模块 docstring 里那份编号的「空测试形态」清单，在末尾追加一条（编号接着现有的往下排）：

```
N. **号称抄自现实、抄的时候字节被悄悄改了。** fixture 的注释写着「真实日志片段，
   取自 <路径>」，但复制过程中有字符被规范化（弯引号→直引号、NBSP→空格、
   全角→半角）。于是 fixture 和被测常量用的是同一个错字符，测试永远绿，
   而现实永远不匹配。2026-09-22 实测：USAGE_LIMIT_MARK 因此死了整整一个版本。
   **防法**：断言的是「对该字符免疫」这个性质（两种写法都要通过），不是某一个字节。
```

- [ ] **Step 2: 写失败的测试**

在 `ERR_USER_LAYER` 旁边加一个弯引号版本，并加两条测试：

```python
# 弯引号版本。**codex 实际输出的就是这个**（U+2019），2026-09-22 从
# ~/.codex-subagent/logs/*.log 按字节核对。上面那条直引号版本留着不是历史包袱：
# 两条一起跑，测的是「判据对引号免疫」这个性质。
ERR_USER_LAYER_CURLY = ERR_USER_LAYER.replace("You've", "You’ve")
```

```python
    def test_撞额度上限_直引号和弯引号都要认(self):
        # codex 用的是 U+2019。判据要是押在某一个引号字符上，换一个就全瞎——
        # 2026-09-22 之前正是这样，死了一整个版本没人发现。
        for name, text in (("直引号", ERR_USER_LAYER), ("弯引号", ERR_USER_LAYER_CURLY)):
            with self.subTest(引号=name):
                v = self._judge(text + "\n")
                self.assertEqual(v.state, "failed")
                self.assertIn("额度", v.reason)

    def test_额度上限的判据里不许有标点(self):
        # 标点是会变的那一类字符（引号、省略号、破折号都有半角全角两套写法）。
        # 判据只用字母和空格，就没有可变的地方。
        self.assertTrue(ca.USAGE_LIMIT_MARK.replace(" ", "").isalpha(),
                        f"判据含非字母字符：{ca.USAGE_LIMIT_MARK!r}")
```

- [ ] **Step 3: 跑测试确认它失败**

```bash
cd /home/xy/.claude/skills/codex-sub-agent
python3 -m unittest test_codex_sub_agent -k 引号 -v
python3 -m unittest test_codex_sub_agent -k 标点 -v
```
预期：两条都 FAIL——弯引号那半匹配不上，常量里有 `'`。

- [ ] **Step 4: 改常量，并把判据抽成共用谓词**

`codex_sub_agent.py` 第 140 行附近：

```python
# 判据**刻意不含 `You've` 那一截**。codex 输出的是弯引号 U+2019，而源码里写的是
# ASCII 直引号——2026-09-22 之前两者对不上，这条特判死了整整一个版本，
# status 把撞上限的任务全报成「报告缺失或为空」。
# 修法不是把直引号换成弯引号（那仍然押在一个会变的字符上），而是**把判据缩短到
# 不含任何标点的那一段**：剩下的全是字母和空格，没有可变的地方。
USAGE_LIMIT_MARK = "hit your usage limit"
```

紧挨着加谓词：

```python
def hit_usage_limit(round_text):
    """本轮是不是撞上了账号额度上限。

    **`judge` 和 `cmd_run` 共用这一个谓词。** 前者拿它决定 reason，后者拿它决定
    换不换账号——两处各写一套检测必然漂移，而判据漂移正是本工具反复在修的那类 bug。
    """
    return USAGE_LIMIT_MARK in round_text
```

把 `judge()` 里的 `if USAGE_LIMIT_MARK in round_text:` 改成 `if hit_usage_limit(round_text):`。

- [ ] **Step 5: 跑测试确认通过**

```bash
python3 -m unittest test_codex_sub_agent -v 2>&1 | tail -5
```
预期：全绿，测试数 252。

- [ ] **Step 6: 拿现网真实日志验一次**

```bash
python3 -c "
import codex_sub_agent as ca, glob, pathlib
for f in glob.glob('/home/xy/.codex-subagent*/logs/*.log'):
    t = pathlib.Path(f).read_text(errors='replace')
    if 'usage limit' in t:
        print(f, '→ hit_usage_limit:', ca.hit_usage_limit(t)); break
"
```
预期：打印 `→ hit_usage_limit: True`。这一步是拿**没经过我的手**的字节验判据，Step 2 的 fixture 验不了这件事。

- [ ] **Step 7: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "fix: USAGE_LIMIT_MARK 用直引号，codex 输出弯引号，从来没匹配过

判据缩短到不含任何标点的那一段，而不是把直引号换成弯引号——后者仍然押在
一个会变的字符上。judge 和 cmd_run 共用 hit_usage_limit 一个谓词，避免漂移。

测试 fixture 号称抄自真实日志，但字节被规范化了，所以两条测试永远绿。
这种空测试形态已补进模块头的清单。"
```

---

### Task 2: 从日志里解析恢复时间

**Files:**
- Modify: `codex_sub_agent.py`（`hit_usage_limit` 下面）
- Test: `test_codex_sub_agent.py`（新建一个 `TestParseResetTime` 类）

**Interfaces:**
- Consumes: 无
- Produces: `parse_reset_time(text) -> (datetime.datetime, str) | None`
  **返回的是元组**：恢复时间和它的原始串必须来自同一次匹配，拆成两个函数就可能各匹配各的、对不上。

- [ ] **Step 1: 写失败的测试**

```python
class TestParseResetTime(unittest.TestCase):
    """真实消息长这样（2026-09-22 从 24 份会话文件取）：
        You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage
        to purchase more credits or try again at Sep 25th, 2026 5:04 PM.
    """

    def test_解析真实消息(self):
        got = ca.parse_reset_time(
            "or try again at Sep 25th, 2026 5:04 PM.")
        self.assertEqual(got[0], datetime.datetime(2026, 9, 25, 17, 4))
        self.assertEqual(got[1], "Sep 25th, 2026 5:04 PM")

    def test_四种序数后缀都要认(self):
        # 1st / 2nd / 3rd / 4th 都会出现，写死一种就会在某些日子失效
        for day, suffix in ((1, "st"), (2, "nd"), (3, "rd"), (4, "th")):
            with self.subTest(日=day):
                got = ca.parse_reset_time(f"try again at Sep {day}{suffix}, 2026 5:04 PM")
                self.assertEqual(got[0].day, day)

    def test_中午和午夜(self):
        # 12 AM = 0 点、12 PM = 12 点。直接 +12 会把两者都算错。
        self.assertEqual(ca.parse_reset_time("try again at Sep 1st, 2026 12:30 AM")[0].hour, 0)
        self.assertEqual(ca.parse_reset_time("try again at Sep 1st, 2026 12:30 PM")[0].hour, 12)

    def test_没这句话就返回None(self):
        self.assertIsNone(ca.parse_reset_time("ERROR: something else entirely"))

    def test_月份名乱写返回None不崩(self):
        # 正则的 [A-Z][a-z]{2} 会匹配 "Foo"，所以月份必须再查一次表
        self.assertIsNone(ca.parse_reset_time("try again at Foo 1st, 2026 5:04 PM"))

    def test_不存在的日期返回None不崩(self):
        self.assertIsNone(ca.parse_reset_time("try again at Feb 31st, 2026 5:04 PM"))

    def test_返回的是朴素时间_不编造时区(self):
        # 消息里没有时区，实测也推不出来（恢复时间在 3~4 天后）。
        # 编一个出来就是在撒谎；这个值只当排序键，朴素时间足够。
        self.assertIsNone(ca.parse_reset_time("try again at Sep 25th, 2026 5:04 PM")[0].tzinfo)
```

文件顶部若还没 `import datetime`，加上。

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 -m unittest test_codex_sub_agent.TestParseResetTime -v
```
预期：全部 ERROR，`module 'codex_sub_agent' has no attribute 'parse_reset_time'`。

- [ ] **Step 3: 实现**

```python
_RESET_AT = re.compile(
    r"try again at\s+"
    r"([A-Z][a-z]{2})\s+"              # Sep
    r"(\d{1,2})(?:st|nd|rd|th),\s+"    # 25th,
    r"(\d{4})\s+"                      # 2026
    r"(\d{1,2}):(\d{2})\s*"            # 5:04
    r"([AP]M)")                        # PM
_MONTHS = {name: number for number, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def parse_reset_time(text):
    """从撞上限的那段话里抠出恢复时间，抠不到就 `None`。

    返回 `(时间, 原始串)`——两样必须来自**同一次匹配**：原始串是落盘时的审计线索
    （解析错了一眼看得出），拆成两个函数就可能各匹配各的、对不上。

    **返回朴素本地时间，不编造时区。** 消息里就没有时区，实测也推不出来
    （恢复时间在 3~4 天后，拿文件 mtime 反推不出来）。这样做安全，是因为这个值
    **只当排序键**（见 `accounts_by_availability`）：误差 ±12h 只在两个账号的恢复
    时间相差 12h 以内时才改变顺序，代价是多一次 codex 启动。有界且良性。

    抠不到宁可返回 `None`——调用方会退化成「无记录＝排最前＝照样会被试到」，
    比写一个假时间进去安全得多。
    """
    m = _RESET_AT.search(text)
    if m is None:
        return None
    month_name, day, year, hour, minute, half = m.groups()
    month = _MONTHS.get(month_name)     # 正则的 [A-Z][a-z]{2} 会匹配 "Foo"，必须再查表
    if month is None:
        return None
    # 12 AM = 0 点、12 PM = 12 点。直接 +12 会把这两个都算错。
    hour = int(hour) % 12 + (12 if half == "PM" else 0)
    try:
        return datetime.datetime(int(year), month, int(day), hour, int(minute)), m.group(0)
    except ValueError:                  # Feb 31st 这种
        return None
```

`codex_sub_agent.py` 顶部若还没 `import datetime`，加上。

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 -m unittest test_codex_sub_agent.TestParseResetTime -v
```
预期：7 条全 PASS。

- [ ] **Step 5: 拿现网真实日志验一次**

```bash
python3 -c "
import codex_sub_agent as ca, glob, pathlib
for f in glob.glob('/home/xy/.codex-subagent*/logs/*.log'):
    t = pathlib.Path(f).read_text(errors='replace')
    if 'usage limit' in t: print(f.split('/')[-1], ca.parse_reset_time(t)); break
"
```
预期：打印出 `(datetime.datetime(2026, 9, 26, 17, 47), 'Sep 26th, 2026 5:47 PM')` 这样的东西。

- [ ] **Step 6: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "feat: 解析额度上限消息里的恢复时间

返回 (时间, 原始串) 元组：两样必须来自同一次匹配，拆开就会对不上。
返回朴素本地时间不编造时区——消息里没有，实测也推不出来；
这个值只当排序键，误差有界且良性。"
```

---

### Task 3: `usage_limit.json` 的读写

**Files:**
- Modify: `codex_sub_agent.py`（`parse_reset_time` 下面）
- Test: `test_codex_sub_agent.py`（新建 `TestUsageLimitFile` 类）

**Interfaces:**
- Consumes: `parse_reset_time`
- Produces: `usage_limit_path(home) -> pathlib.Path`、`write_usage_limit(home, reset_at, raw) -> None`、`read_usage_limit(home) -> datetime.datetime | None`

- [ ] **Step 1: 写失败的测试**

```python
class TestUsageLimitFile(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def test_写了能读回来(self):
        when = datetime.datetime(2026, 9, 25, 17, 4)
        ca.write_usage_limit(self.home, when, "Sep 25th, 2026 5:04 PM")
        self.assertEqual(ca.read_usage_limit(self.home), when)

    def test_键只有两个(self):
        # seen_at 之类没有消费者的字段一旦加进去就再也删不掉了
        ca.write_usage_limit(self.home, datetime.datetime(2026, 9, 25, 17, 4), "raw")
        got = json.loads(ca.usage_limit_path(self.home).read_text())
        self.assertEqual(set(got), {"reset_at", "raw"})

    def test_raw原样保留(self):
        # raw 是审计线索：解析错了要能一眼看出错在哪，所以不许加工
        ca.write_usage_limit(self.home, datetime.datetime(2026, 9, 25, 17, 4),
                             "Sep 25th, 2026 5:04 PM")
        got = json.loads(ca.usage_limit_path(self.home).read_text())
        self.assertEqual(got["raw"], "Sep 25th, 2026 5:04 PM")

    def test_文件不存在读出None(self):
        self.assertIsNone(ca.read_usage_limit(self.home))

    def test_坏json读出None不崩(self):
        # 读失败退化成「无记录＝排最前＝照样会被试到」，这是安全的那一边
        ca.usage_limit_path(self.home).write_text("{不是 json")
        self.assertIsNone(ca.read_usage_limit(self.home))

    def test_时间串坏了读出None不崩(self):
        ca.usage_limit_path(self.home).write_text('{"reset_at": "昨天", "raw": "x"}')
        self.assertIsNone(ca.read_usage_limit(self.home))

    def test_缺键读出None不崩(self):
        ca.usage_limit_path(self.home).write_text('{"raw": "x"}')
        self.assertIsNone(ca.read_usage_limit(self.home))

    def test_原子替换_读者看不到半截(self):
        # 和 write_meta 同一个理由：另一个账号的 run 可能同时在读。
        # 写的过程中目标文件要么是旧的完整内容，要么是新的完整内容。
        ca.write_usage_limit(self.home, datetime.datetime(2026, 9, 25, 17, 4), "旧")
        seen = []
        real_replace = os.replace
        def spy(src, dst):
            seen.append(json.loads(pathlib.Path(dst).read_text())["raw"])
            return real_replace(src, dst)
        with unittest.mock.patch("os.replace", spy):
            ca.write_usage_limit(self.home, datetime.datetime(2026, 9, 26, 17, 47), "新")
        self.assertEqual(seen, ["旧"], "替换发生前目标文件必须还是完整的旧内容")
```

文件顶部若还没 `import tempfile, shutil, json, os, unittest.mock`，加上。

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 -m unittest test_codex_sub_agent.TestUsageLimitFile -v
```
预期：全部 ERROR，没有 `write_usage_limit` 这个属性。

- [ ] **Step 3: 实现**

```python
USAGE_LIMIT_FILE = "usage_limit.json"


def usage_limit_path(home):
    """限流记录放隔离目录根部。

    限流是**账号**的属性，而账号已经有一个家——不必为它新开一个状态目录。
    `ensure_isolation` 只要求子目录存在、`config.toml` 是普通文件、`auth.json` 是
    软链，**不拒绝多余文件**，所以放这里合法（2026-09-22 读代码确认）。
    """
    return home / USAGE_LIMIT_FILE


def write_usage_limit(home, reset_at, raw):
    """记下这个账号什么时候恢复。**只在 `parse_reset_time` 成功时调用。**

    `raw` 是审计线索：解析错了（时区、OpenAI 改文案）一眼看得出，不用回去翻日志。
    **没有 `seen_at`**——唯一的读者是排序，排序只看 `reset_at`；没有消费者的字段
    一旦加进去就再也删不掉了。

    原子替换的理由和 `write_meta` 逐字相同：另一个账号的 `run` 可能正在读同一份
    文件，原地截断重写会让它读到半截 json。
    """
    q = usage_limit_path(home)
    tmp = q.with_name(f"{q.name}.tmp")
    tmp.write_text(json.dumps({"reset_at": reset_at.isoformat(), "raw": raw},
                              ensure_ascii=False, indent=2))
    os.replace(tmp, q)


def read_usage_limit(home):
    """这个账号预计什么时候恢复；没记录或记录坏了都返回 `None`。

    **坏了返回 `None` 是刻意的**：调用方会把 `None` 当成「无记录＝排最前＝照样会
    被试到」，那是安全的那一边。反过来（读不出来就认定它还在限流）会把一个可用
    账号锁死，而调用方看不出原因——那正是本工具反复在修的那类谎。
    """
    try:
        got = json.loads(usage_limit_path(home).read_text())
        return datetime.datetime.fromisoformat(got["reset_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 -m unittest test_codex_sub_agent.TestUsageLimitFile -v
```
预期：8 条全 PASS。

- [ ] **Step 5: 确认没破坏隔离目录的不变量**

```bash
python3 -c "
import codex_sub_agent as ca, datetime
home = ca.ensure_isolation('acct2')
ca.write_usage_limit(home, datetime.datetime(2026,1,1,0,0), 'smoke test')
ca.ensure_isolation('acct2')          # 多了个文件之后再跑一次，不许拒
print('ensure_isolation 不介意多余文件：OK')
ca.usage_limit_path(home).unlink()    # 清掉冒烟测试留下的假记录
"
```
预期：打印 OK 且不抛异常。**最后那行 unlink 不能漏**，否则会给 acct2 留一条假的限流记录。

- [ ] **Step 6: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "feat: usage_limit.json —— 每个隔离目录记一条恢复时间

只有 reset_at 和 raw 两个键，没有 seen_at（没有消费者）。
读失败一律 None，退化成「无记录＝排最前＝照样会被试到」，
那是安全的那一边；反过来会把可用账号锁死且调用方看不出原因。
原子替换理由同 write_meta。"
```

---

### Task 4: 按可用性给账号排序

**Files:**
- Modify: `codex_sub_agent.py`（`account_choices` 下面）
- Test: `test_codex_sub_agent.py`（新建 `TestAccountOrder` 类）

**Interfaces:**
- Consumes: `account_choices`、`isolation_home`、`find_meta`、`read_usage_limit`
- Produces: `AUTO = "auto"`、`accounts_by_availability(task) -> list[str]`

- [ ] **Step 1: 给 `account_choices` 加保留字护栏，并写它的测试**

`auto` 要进 `--account` 的可选项，所以真账号里不许有叫 `auto` 的，否则 `--account auto` 的含义就有两种。这是「把约束写进代码」，不是防御性编程。

测试：

```python
    def test_账号目录叫auto要拒跑(self):
        # auto 是 --account 的保留字。真账号也叫 auto，那个参数就有两种含义了。
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / ".codex-accounts" / "auto").mkdir(parents=True)
        with unittest.mock.patch.object(pathlib.Path, "home", lambda: d):
            with self.assertRaises(ca.Rejected) as got:
                ca.account_choices()
        self.assertIn("auto", str(got.exception.message))
```

实现——在 `account_choices()` 里那个 `for name in extra:` 循环体内，坏字符检查之后加：

```python
        if name == AUTO:
            reject(f"账号目录 {accounts_dir / name} 叫 {AUTO}，而 {AUTO} 是 "
                   f"`--account` 的保留字（表示自动分配）。两种含义撞在同一个词上，"
                   f"调用方无从分辨。把这个目录改个名。")
```

- [ ] **Step 2: 写排序的失败测试**

```python
class TestAccountOrder(unittest.TestCase):
    """排序只影响**先试谁**，不影响**试不试**。所以这里的每条断言都是关于顺序的，
    没有一条是关于「某个账号被排除了」——那种事在本设计里不存在。
    """

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for name in ("acct2", "acct3"):
            (self.root / ".codex-accounts" / name).mkdir(parents=True)
        patcher = unittest.mock.patch.object(pathlib.Path, "home", lambda: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        for account in ("default", "acct2", "acct3"):
            (ca.isolation_home(account) / "tasks").mkdir(parents=True)

    def _limit(self, account, when):
        ca.write_usage_limit(ca.isolation_home(account), when, "测试写的")

    def test_没记录的排在有记录的前面(self):
        self._limit("default", datetime.datetime(2099, 1, 1))
        self._limit("acct3", datetime.datetime(2098, 1, 1))
        self.assertEqual(ca.accounts_by_availability("t")[0], "acct2")

    def test_有记录的按恢复时间升序(self):
        self._limit("default", datetime.datetime(2099, 1, 2))
        self._limit("acct2", datetime.datetime(2099, 1, 1))
        self._limit("acct3", datetime.datetime(2099, 1, 3))
        self.assertEqual(ca.accounts_by_availability("t"), ["acct2", "default", "acct3"])

    def test_全都没记录时按账号名_顺序必须确定(self):
        # 不写第二键的话，顺序跟着 account_choices 的扫描顺序漂，
        # 同一个输入在两台机器上给出两种结果。
        self.assertEqual(ca.accounts_by_availability("t"), ["acct2", "acct3", "default"])

    def test_已存在的任务名_它的账号排最前(self):
        # 即使那个账号有一条最晚的恢复记录，也照样排最前：沿用它的家。
        ca.write_meta(ca.isolation_home("acct3"), "t",
                      ca.new_meta("t", "acct3", "/tmp", "high", []))
        self._limit("acct3", datetime.datetime(2099, 12, 31))
        self.assertEqual(ca.accounts_by_availability("t")[0], "acct3")

    def test_恢复时间已经过去的_和没记录的一样靠前(self):
        # 不需要过期清理：时间一过它自然排到前面去。
        self._limit("default", datetime.datetime(2000, 1, 1))
        self._limit("acct2", datetime.datetime(2099, 1, 1))
        self._limit("acct3", datetime.datetime(2099, 1, 2))
        self.assertEqual(ca.accounts_by_availability("t")[0], "default")

    def test_一个都不少(self):
        # **这条是本设计的核心约束。** 排序永远返回全部账号，
        # 「全部账号都满了」这个结论只能来自真实尝试，不能来自排序函数。
        for account in ("default", "acct2", "acct3"):
            self._limit(account, datetime.datetime(2099, 1, 1))
        self.assertEqual(sorted(ca.accounts_by_availability("t")),
                         sorted(ca.account_choices()))
```

- [ ] **Step 3: 跑测试确认它失败**

```bash
python3 -m unittest test_codex_sub_agent.TestAccountOrder -v
```
预期：全部 ERROR，没有 `accounts_by_availability`。

- [ ] **Step 4: 实现**

```python
AUTO = "auto"


def accounts_by_availability(task):
    """按「预计什么时候能用」给候选账号排序。**返回全部账号，一个都不少。**

    这个函数**只排顺序，不准拒绝**——本设计最重要的一条约束，别在这里加过滤。
    「全部账号都撞上额度上限」这个结论必须来自**真实尝试**（见 `cmd_run` 的循环），
    不能来自这里读到的文件。文件会错（时区、时钟偏移、OpenAI 改文案），
    只排顺序的话最坏后果被钉死在「顺序排差、多一次 codex 启动」；
    一旦允许它拒绝，同一个错就变成「把好账号误判成死的、还告诉调用方没号可用」。

    排序键三段：
    ① 这个任务是不是已经住在该账号——住着的排最前（沿用它的家），
       但仍可被 `cmd_run` 的重试搬走
    ② 恢复时间；没记录就是 `datetime.min`，排最前，照样会被试到
    ③ 账号名——**第二键必须有**，否则顺序跟着 `account_choices()` 的扫描顺序漂，
       同一个输入在两台机器上给出两种结果
    """
    own_home, _ = find_meta(task)
    def sort_key(account):
        home = isolation_home(account)
        return (0 if home == own_home else 1,
                read_usage_limit(home) or datetime.datetime.min,
                account)
    return sorted(account_choices(), key=sort_key)
```

- [ ] **Step 5: 跑测试确认通过**

```bash
python3 -m unittest test_codex_sub_agent.TestAccountOrder -v
python3 -m unittest test_codex_sub_agent -v 2>&1 | tail -3
```
预期：新类全 PASS，全量仍全绿。

- [ ] **Step 6: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "feat: 按恢复时间给账号排序，并给 account_choices 加 auto 保留字护栏

排序只排顺序不准拒绝——这是本设计最重要的约束。文件写错时最坏后果
钉死在多一次启动，而不是把好账号误判成死的。
第二键用账号名：不写的话顺序跟着扫描顺序漂，同一输入两台机器两种结果。"
```

---

### Task 5: `--account auto` 与撞上限换号重试

**Files:**
- Modify: `codex_sub_agent.py` —— `cmd_run()` 整个重写、`build_parser()` 里 run 的 `--account` 那一行
- Test: `test_codex_sub_agent.py`（新建 `TestRunRetry` 类）

**Interfaces:**
- Consumes: 前四个 Task 的全部产物
- Produces: 无新公开函数；`cmd_run` 行为变化

**背景（实现者必读）：** `run_codex()` 在 spawn **之前**就把元数据写进了该账号的 home。而 `cmd_run` 有一道护栏：同名任务不许跨账号（否则会留下够不着的孤儿元数据）。所以换账号重试前**必须先删掉上一个账号的元数据**，否则第二次尝试会被自己的护栏挡住。删掉之后不变量恢复：**一个任务名在任何时刻只属于一个隔离目录。**

日志**不删**：它是撞上限的证据，而恢复时间已经进了 `usage_limit.json`，日志本身冗余。下次同名任务跑在这个账号上时 `run_codex` 追加写，`read_round` 按字节偏移切本轮，不会被旧内容污染。

- [ ] **Step 1: 写失败的测试**

```python
class TestRunRetry(unittest.TestCase):
    """全部用假的 `run_codex` —— 真跑 codex 既慢又要花钱，而这里要测的是
    **循环的控制流**，不是 codex 本身。
    """

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for name in ("acct2", "acct3"):
            (self.root / ".codex-accounts" / name).mkdir(parents=True)
            (self.root / ".codex-accounts" / name / "auth.json").write_text("{}")
        (self.root / ".codex").mkdir()
        (self.root / ".codex" / "auth.json").write_text("{}")
        (self.root / "work").mkdir()
        self.brief = self.root / "brief.md"
        self.brief.write_text("干活\n")
        patcher = unittest.mock.patch.object(pathlib.Path, "home", lambda: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tried = []

    def _args(self, account):
        return argparse.Namespace(task="t", dir=str(self.root / "work"),
                                  brief=str(self.brief), effort="high",
                                  account=account, skills=[])

    def _fake_run(self, outcomes):
        """outcomes: 账号 → 本轮日志文本。按账号决定这一轮的结果。"""
        def fake(kind, home, task, meta, make_argv):
            account = meta["account"]
            self.tried.append(account)
            ca.write_meta(home, task, meta)          # 真 run_codex 也是先写元数据
            report = ca._report_path(home, task)
            text = outcomes[account]
            if "usage limit" not in text:
                report.write_text("干完了\n")
            return ca.Round(report, text)
        return fake

    LIMIT_ACCT2 = "ERROR: You’ve hit your usage limit. … try again at Sep 25th, 2026 5:04 PM."
    LIMIT_ACCT3 = "ERROR: You’ve hit your usage limit. … try again at Sep 26th, 2026 5:47 PM."
    FINE = "一切正常\n"

    def test_第一个撞上限就换下一个(self):
        fake = self._fake_run({"acct2": self.LIMIT_ACCT2, "acct3": self.FINE,
                               "default": self.FINE})
        with unittest.mock.patch.object(ca, "run_codex", fake):
            code = ca.cmd_run(self._args(ca.AUTO))
        self.assertEqual(self.tried[:2], ["acct2", "acct3"])
        self.assertEqual(code, ca.EXIT["success"])

    def test_换号前把上一个账号的元数据删掉(self):
        # 不删的话，第二次尝试会被 cmd_run 自己的跨账号同名护栏挡住。
        # 不变量：一个任务名在任何时刻只属于一个隔离目录。
        fake = self._fake_run({"acct2": self.LIMIT_ACCT2, "acct3": self.FINE,
                               "default": self.FINE})
        with unittest.mock.patch.object(ca, "run_codex", fake):
            ca.cmd_run(self._args(ca.AUTO))
        self.assertFalse(ca.meta_path(ca.isolation_home("acct2"), "t").exists())
        self.assertTrue(ca.meta_path(ca.isolation_home("acct3"), "t").exists())

    def test_撞上限要把恢复时间记下来(self):
        fake = self._fake_run({"acct2": self.LIMIT_ACCT2, "acct3": self.FINE,
                               "default": self.FINE})
        with unittest.mock.patch.object(ca, "run_codex", fake):
            ca.cmd_run(self._args(ca.AUTO))
        self.assertEqual(ca.read_usage_limit(ca.isolation_home("acct2")),
                         datetime.datetime(2026, 9, 25, 17, 4))

    def test_全撞上限_退出码是failed且逐个列出恢复时间(self):
        fake = self._fake_run({"acct2": self.LIMIT_ACCT2, "acct3": self.LIMIT_ACCT3,
                               "default": self.LIMIT_ACCT3})
        with unittest.mock.patch.object(ca, "run_codex", fake), \
             unittest.mock.patch("sys.stdout", io.StringIO()) as out:
            code = ca.cmd_run(self._args(ca.AUTO))
        self.assertEqual(code, ca.EXIT["failed"])
        printed = out.getvalue()
        for account in ("acct2", "acct3", "default"):
            self.assertIn(account, printed)
        self.assertIn("09-25", printed)

    def test_尝试次数上界是账号数(self):
        fake = self._fake_run({"acct2": self.LIMIT_ACCT2, "acct3": self.LIMIT_ACCT3,
                               "default": self.LIMIT_ACCT3})
        with unittest.mock.patch.object(ca, "run_codex", fake), \
             unittest.mock.patch("sys.stdout", io.StringIO()):
            ca.cmd_run(self._args(ca.AUTO))
        self.assertEqual(len(self.tried), len(ca.account_choices()))

    def test_不是额度问题就不换号(self):
        # 换号救不了「代码写错了」这种失败，白烧一轮。只有额度问题才换。
        fake = self._fake_run({"acct2": "ERROR: something broke", "acct3": self.FINE,
                               "default": self.FINE})
        with unittest.mock.patch.object(ca, "run_codex", fake), \
             unittest.mock.patch("sys.stdout", io.StringIO()):
            ca.cmd_run(self._args(ca.AUTO))
        self.assertEqual(self.tried, ["acct2"])

    def test_指定账号撞上限_记录但不重试(self):
        fake = self._fake_run({"acct2": self.LIMIT_ACCT2, "acct3": self.FINE,
                               "default": self.FINE})
        with unittest.mock.patch.object(ca, "run_codex", fake), \
             unittest.mock.patch("sys.stdout", io.StringIO()):
            code = ca.cmd_run(self._args("acct2"))
        self.assertEqual(self.tried, ["acct2"], "强制模式不许换号")
        self.assertEqual(code, ca.EXIT["failed"])
        # 但事实照记：强制模式踩到的坑要让 auto 模式变聪明
        self.assertEqual(ca.read_usage_limit(ca.isolation_home("acct2")),
                         datetime.datetime(2026, 9, 25, 17, 4))

    def test_成功那一轮的账号里留着元数据(self):
        fake = self._fake_run({"acct2": self.FINE, "acct3": self.FINE, "default": self.FINE})
        with unittest.mock.patch.object(ca, "run_codex", fake), \
             unittest.mock.patch("sys.stdout", io.StringIO()):
            ca.cmd_run(self._args(ca.AUTO))
        homes = [h for h, _ in ca.all_metas()]
        self.assertEqual(len(homes), 1, "一个任务名只能住一个隔离目录")
```

文件顶部若还没 `import argparse, io`，加上。

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 -m unittest test_codex_sub_agent.TestRunRetry -v
```
预期：全部 FAIL/ERROR（`ca.AUTO` 不存在、没有重试逻辑）。

- [ ] **Step 3: 改 parser**

```python
    r.add_argument("--account", required=True, choices=account_choices() + [AUTO],
                   help=f"codex 账号；{AUTO} = 自动挑一个没在限流的，撞上额度上限就换下一个")
```

`--account` **仍然必填**：没有任何隐式选中的值。

- [ ] **Step 4: 重写 `cmd_run`**

前面的 `--dir` / `--brief` 校验**一行不改**，从 `home = isolation_home(args.account)` 那行起换成：

```python
    old_home, old_meta = find_meta(args.task)
    if old_meta is not None:
        # 「还在跑」只查一次，进循环之前：它问的是「这个任务名此刻有没有活着的
        # codex」，和试哪个账号无关。
        if find_codex_pid(_report_path(old_home, args.task)) is not None:
            reject(f"任务名 {args.task} 还在跑，换个名字或先 `codex-sub-agent stop {args.task}`")
        print(f"[codex-sub-agent] 提示：任务名 {args.task} 复用，上一轮的报告会被删掉、日志会被追加")

    if args.account == AUTO:
        candidates = accounts_by_availability(args.task)
    else:
        # 跨账号同名的护栏**只在强制模式下需要**：auto 模式里，任务已有的那个家
        # 排在最前（第一轮就对上），而重试前元数据已经删掉（后面几轮无从撞车）。
        if old_meta is not None and old_home != isolation_home(args.account):
            reject(f"任务名 {args.task} 已经属于账号 {old_meta['account']}（{old_home}）。\n"
                   f"同名任务跨账号会让 status/resume/stop 指向哪个变得不确定，换个任务名。")
        candidates = [args.account]

    brief = prepend_skill_guard(brief_file.read_text(), args.skills)
    exhausted = []          # [(账号, 恢复时间 or None)]，全撞上限时要逐个报出来
    for attempt, account in enumerate(candidates):
        home = ensure_isolation(account)
        round = run_codex("run", home, args.task,
                          new_meta(args.task, account, str(workdir), args.effort, args.skills),
                          lambda r: build_run_argv(str(workdir), args.effort, r, brief))
        verdict = judge(round)
        limited = verdict.state == "failed" and hit_usage_limit(round.text)
        if limited:
            # 解析失败也要记进 exhausted（值为 None）：这个账号确实满了，
            # 只是不知道什么时候恢复。漏记会让「全撞上限」那条汇总少一行。
            hit = parse_reset_time(round.text)
            if hit is not None:
                write_usage_limit(home, *hit)
            exhausted.append((account, hit[0] if hit else None))
            if args.account == AUTO and attempt + 1 < len(candidates):
                # run_codex 在开跑前就把元数据写进了这个 home。不删的话，
                # 下一个账号会被上面那道跨账号同名护栏挡住（强制模式那支），
                # 而且这一份会变成再也够不着的孤儿。
                # 不变量：一个任务名在任何时刻只属于一个隔离目录。
                meta_path(home, args.task).unlink(missing_ok=True)
                when = f"，恢复于 {hit[0]:%m-%d %H:%M}" if hit else ""
                print(f"[codex-sub-agent] {account} 撞上额度上限{when}，换下一个账号")
                continue
        if len(exhausted) > 1:
            # 不新增状态，只把 reason 说清楚——沿用 judge 的规矩。
            verdict = Verdict("failed", f"全部 {len(exhausted)} 个账号都撞上额度上限",
                              [f"{a:<8} {'恢复于 ' + t.strftime('%m-%d %H:%M') if t else '恢复时间没解析出来'}"
                               for a, t in exhausted])
        _print_verdict(args.task, verdict)
        print(f"  报告 {_report_path(home, args.task)}\n  日志 {_log_path(home, args.task)}")
        return EXIT[verdict.state]
```

注意 `ensure_isolation(account)` 返回的就是 `home`（读它最后一行），所以不必再调一次 `isolation_home`。

- [ ] **Step 5: 跑测试确认通过**

```bash
python3 -m unittest test_codex_sub_agent.TestRunRetry -v
python3 -m unittest test_codex_sub_agent -v 2>&1 | tail -3
```
预期：新类 8 条全 PASS，全量全绿且总数 ≥ 250。

- [ ] **Step 6: 真跑一次冒烟**

```bash
cd /home/xy/.claude/skills/codex-sub-agent
printf '回答一个字：好\n' > /tmp/smoke-brief.md
./codex_sub_agent.py run --task smoke-auto --dir /tmp \
  --brief /tmp/smoke-brief.md --effort low --account auto --no-skill
echo "退出码 $?"
```
预期：打印 `[分配]` 或直接开跑，最终 `success`，退出码 0。**当前 default 和 acct3 都在限流中（恢复时间分别是 9/26 17:47 和 9/25 17:04），所以 auto 应该直接选中 acct2。**

- [ ] **Step 7: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "feat: --account auto —— 自动挑账号，撞上额度上限就换下一个

重试前必须删掉上一个账号的元数据：run_codex 在 spawn 前就写了它，
不删的话第二次尝试被 cmd_run 自己的跨账号同名护栏挡住，而且会留下
够不着的孤儿。不变量：一个任务名在任何时刻只属于一个隔离目录。

强制模式撞上限照样记录恢复时间但不重试——那是关于账号的事实，
与调用方式无关；强制模式踩到的坑应该让 auto 模式变聪明。

全部账号都满了仍是 failed，只是 reason 更具体，不新增状态码。"
```

---

### Task 6: 文档同步

**Files:**
- Modify: `SKILL.md`（账号那一行，第 68 行附近）
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 5 的 CLI 行为
- Produces: 无

**注意：** `SKILL.md` 工作区里有一处**未提交**的改动，补的是「一个账号可以同时开多个子代理」，改的正是要重写的同一行。**把这句话并进新版本，不要单独提交、也不要丢掉。**

- [ ] **Step 1: 改 `SKILL.md` 的账号那一行**

原文：
```
| 账号 | `--account` 在 `default` / `acct2` / `acct3` 之间选（可选项由 `~/.codex-accounts/` 扫出来，加一个账号就自动认）。**一个账号可以同时开多个子代理**，不用排队等前一个跑完——换账号的唯一理由是**撞上额度上限**，判据会直接告诉你是额度问题。每个账号有各自独立的隔离目录，互不干扰 |
```

改成：
```
| 账号 | `--account auto` —— **默认就写这个**，工具自己挑一个没在限流的，撞上额度上限会自动换下一个重试，全部满了才报错并告诉你每个账号什么时候恢复。**一个账号可以同时开多个子代理**，不用排队等前一个跑完。要盯某一个账号就写它的名字（`default` / `acct2` / `acct3`，由 `~/.codex-accounts/` 扫出来，加一个账号就自动认），那样撞上限**不会**换号。每个账号有各自独立的隔离目录，互不干扰 |
```

- [ ] **Step 2: 改 `README.md`**

```bash
grep -n "account" README.md
```
把所有 `--account <账号>` 的示例改成 `--account auto`，并在参数说明里加一行：

```
| `--account` | `auto` 自动挑（撞额度上限自动换号重试），或写死某个账号名 |
```

- [ ] **Step 3: 核对文档和 CLI 真的一致**

```bash
cd /home/xy/.claude/skills/codex-sub-agent
./codex_sub_agent.py run --help | grep -A2 -- --account
grep -n "auto" SKILL.md README.md
```
预期：三处说的是同一件事。

- [ ] **Step 4: 提交**

```bash
git add SKILL.md README.md
git commit -m "docs: --account auto

并入工作区里那处未提交的改动（一个账号可以同时开多个子代理），
它改的是同一行。"
```

---

## Self-Review 结果

**Spec 覆盖**（逐节核对）：

| spec 的要求 | 哪个 Task |
|---|---|
| 判据缩短到不含标点 + 引号免疫测试 | Task 1 |
| 第十种空测试形态补进清单 | Task 1 Step 1 |
| `usage_limit.json` 只有两个键、无过期清理 | Task 3 |
| 解析失败不写文件 | Task 2（返回 None）+ Task 5（`if hit is not None`） |
| 时区不编造 | Task 2 Step 1 最后一条测试 |
| 排序键 `(是否住在该账号, reset_at, 账号名)` | Task 4 |
| 状态只排顺序不准拒绝 | Task 4 的 `test_一个都不少` |
| 撞上限删元数据再换号 | Task 5 的 `test_换号前把上一个账号的元数据删掉` |
| 尝试上界 = 账号数 | Task 5 的 `test_尝试次数上界是账号数` |
| 全满 → failed + 逐个列出恢复时间 | Task 5 |
| 强制模式记录但不重试 | Task 5 的 `test_指定账号撞上限_记录但不重试` |
| `--account` 仍必填、取值含 auto | Task 5 Step 3 |
| 跨账号同名护栏不用改 | Task 5 Step 4（挪进强制模式那一支，逻辑等价） |
| 复用 `judge`，不另写检测 | Task 1 的 `hit_usage_limit` 共用谓词 |
| 「还在跑」只查一次 | Task 5 Step 4（hoist 到循环外） |
| `ensure_isolation` 每次尝试都跑 | Task 5 Step 4（循环体内） |
| 重试用同一份 brief/effort/skills | Task 5 Step 4（`brief` 在循环外算一次） |
| `status` 不加账号行、不新增状态码 | 全程不碰 `cmd_status`、`_STATES` |
| 文档同步 + 并入未提交的那一行 | Task 6 |

**无遗漏。**

**占位符扫描：** 无 TBD/TODO；每个代码步骤都有可直接粘贴的真代码；每条测试都有真断言。

**类型一致性：** `parse_reset_time` 在 Task 2 定义为返回 `(datetime, str) | None`，Task 5 按 `hit[0]` 和 `write_usage_limit(home, *hit)` 用——一致。`accounts_by_availability(task)` 在 Task 4 定义收任务名，Task 5 传 `args.task`——一致。`ensure_isolation(account)` 返回 `home`（读代码确认最后一行是 `return d`），Task 5 Step 4 直接用返回值——一致。
