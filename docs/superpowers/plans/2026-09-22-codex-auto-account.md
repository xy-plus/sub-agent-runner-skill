# codex-sub-agent 账号自动分配 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `--account auto` 让工具自己挑账号，撞上额度上限就记下恢复时间并换下一个重试。

**Architecture:** 五层，自下而上互不依赖。① 建在**已分类错误行**上的额度谓词；② 恢复时间解析；③ `usage_limit.json` 读写；④ 按可用性排序 + 保留名护栏；⑤ `cmd_run` 重试循环。核心约束：**额度记录只排顺序，不准拒绝**——「全部账号都满了」永远来自真实尝试。

**Tech Stack:** Python 3 标准库（`re` / `json` / `datetime` / `pathlib` / `os`），`unittest`。无第三方依赖，且不许新增。

> **本计划是 v2**，按 spec 审查结论重写。v1 的额度谓词建在整轮自由文本上，会把「brief 里提到这句话」误判成额度问题，进而删元数据、换账号重跑。

## Global Constraints

- 额度谓词**只看 `runtime_error_lines()` 的返回值**，绝不对整轮文本做裸子串匹配
- `USAGE_LIMIT_MARK` 的值必须是 `"hit your usage limit"`——**不含任何标点字符**
- 状态文件 `usage_limit.json` 放隔离目录根部，**键只有 `reset_at` 和 `raw`**，临时文件名**带 pid**
- `reset_at` 存**朴素本地时间**的 ISO 串，不带时区
- 排序键 `(是否已住在该账号, reset_at, 账号名)`，无记录用 `datetime.min`
- `--account` **仍然必填**，`choices` = `account_choices()` + `["auto"]`
- `default` 和 `auto` 都是**保留名**，`~/.codex-accounts/` 下有同名目录就拒跑
- **不新增状态码，不给 `Verdict` 加字段**
- **凡需要 HOME 沙箱的测试类一律继承 `_HomeSandbox`，不许手写第二份。** 它同时 patch `Path.home()` 和 `$HOME`，两条缺一不可——手写的那份必然漏掉 `$HOME`，而漏掉之后测试**不会红**，只是悄悄读写**真实**的 `~/.codex-subagent`
- patch 一律写 `mock.patch`（顶部是 `from unittest import mock`，现有 99 处都这么写）
- 临时目录用 `pathlib.Path(tempfile.mkdtemp())`，**不加 rmtree 清理**——现有 8 处都这样，不引入 `shutil`
- **不设测试数量门槛。** 基线 250 条本来就满足任何门槛，证明不了新功能，且与仓库规范第 7 条「积极删除或合并」冲突
- 每个新函数都要有中文 docstring 说明**为什么这么做**
- 提交时**不要传** `-c user.email` / `-c user.name`，也不要设 `GIT_AUTHOR_*` / `GIT_COMMITTER_*`

---

## File Structure

| 文件 | 职责 | 为什么不拆 |
|---|---|---|
| `codex_sub_agent.py` | 全部实现（新增约 110 行） | 单文件 CLI 是既有形态，拆出去会让「一个可执行文件即全部」这个性质消失 |
| `test_codex_sub_agent.py` | 全部测试 | 同上 |
| `SKILL.md` / `README.md` | 文档同步 | 面向不同读者（skill 激活时 / GitHub 首页），都要改 |

---

### Task 1: 额度判据——既匹配得上真的，又匹配不上假的

**Files:**
- Modify: `codex_sub_agent.py:140`（常量）、`judge()` 里用它的那一行
- Modify: `test_codex_sub_agent.py`（模块 docstring 的清单、`ERR_USER_LAYER` 附近、judge 的测试类）

**Interfaces:**
- Consumes: 现有的 `runtime_error_lines(round_text)`
- Produces: `USAGE_LIMIT_MARK = "hit your usage limit"`、`hit_usage_limit(error_lines) -> bool`

**背景（实现者必读）：** 现有判据有**两个**毛病，只修一个没用。

1. **匹配不上真的。** 常量写的是 `"You've hit your usage limit"`，ASCII 直引号 U+0027；codex 实际输出弯引号 U+2019。全量核对过：27 份日志、54 行额度错误全是弯引号。所以这条特判是死代码，`status` 把撞上限报成「报告缺失或为空」。
2. **匹配得上假的，而且这条更严重。** 判据是 `USAGE_LIMIT_MARK in round_text`——对**整轮自由文本**做裸子串匹配。同一份源码在 `_ERR_*` 正则上方自己写着「日志里混着 brief 原文和 codex 转述的子进程输出，绝不能用裸 grep，会大面积误报」。一个 brief 里提到这句话、实际却是被打断的任务，会被判成额度问题——在本次改动之后，那意味着**删元数据、换账号重跑整个任务**。

- [ ] **Step 1: 把这种空测试形态记进模块头的清单**

打开 `test_codex_sub_agent.py` 模块 docstring 里那份编号清单（**当前编到 7**），追加第 8 条：

```
    8. **fixture 与被测常量共享同一个未经现实核对的假设。** 2026-09-22：
       USAGE_LIMIT_MARK 写的是 ASCII 直引号，codex 输出的是弯引号 U+2019，
       判据一次都没匹配上过；而 fixture 恰好也用直引号，于是测试照常绿。
       **注意它不是「什么都不测」**——把匹配逻辑整个禁掉，两条测试都会红。
       它测不出的是**另一类突变**：改掉引号字符（也就是现实里真正发生的那种偏差），
       测试毫无反应。防法不是多写断言，而是让断言测**性质**
       （两种引号都要通过），并引入**未经我手的真实字节**（见 Step 6）。
```

- [ ] **Step 2: 写失败的测试**

在 `ERR_USER_LAYER` 旁边加弯引号版本：

```python
# 弯引号版本。**codex 实际输出的就是这个**（U+2019），2026-09-22 对 27 份日志、
# 54 行额度错误全量核对，无一例外。上面那条直引号版本留着不是历史包袱：
# 两条一起跑，测的是「判据对引号免疫」这个性质。
ERR_USER_LAYER_CURLY = ERR_USER_LAYER.replace("You've", "You’ve")
```

在 judge 的测试类里加四条：

```python
    def test_撞额度上限_直引号和弯引号都要认(self):
        # 判据要是押在某一个引号字符上，换一个就全瞎——2026-09-22 之前正是这样。
        for name, text in (("直引号", ERR_USER_LAYER), ("弯引号", ERR_USER_LAYER_CURLY)):
            with self.subTest(引号=name):
                v = self._judge(text + "\n")
                self.assertEqual(v.state, "failed")
                self.assertIn("额度", v.reason)

    def test_额度判据里不许有标点(self):
        # 标点是会变的那一类字符（引号有半角全角两套写法）。判据只用字母和空格。
        self.assertTrue(ca.USAGE_LIMIT_MARK.replace(" ", "").isalpha(),
                        f"判据含非字母字符：{ca.USAGE_LIMIT_MARK!r}")

    def test_brief里提到额度但实际是被打断_不许判成额度问题(self):
        # **本 Task 最重要的一条。** 日志里混着 brief 原文和 codex 转述的子进程输出，
        # 对整轮自由文本做子串匹配会把它们当成 codex 自己的错误。
        # 判成额度问题的后果不再只是「解释错了」——cmd_run 会据此删元数据、换账号重跑。
        brief_echo = "任务：排查为什么会 hit your usage limit\n"
        v = self._judge(brief_echo + ca.INTERRUPT_MARK + "\n")
        self.assertEqual(v.state, "interrupted")
        self.assertNotIn("额度", v.reason)

    def test_判据过宽时上面那条负例必须变红(self):
        # 「不含标点」挡不住一个过宽的常量（比如单个字母）。这条钉的是：
        # 负例的红是真的红，不是因为判据恰好没被触发。
        with mock.patch.object(ca, "USAGE_LIMIT_MARK", "a"):
            v = self._judge("任务：排查为什么会 hit your usage limit\n"
                            + ERR_USER_LAYER + "\n")
            self.assertIn("额度", v.reason, "判据过宽时应当误判——这条红了说明负例失效")
```

- [ ] **Step 3: 跑测试确认它们失败**

```bash
cd /home/xy/.claude/skills/codex-sub-agent
python3 -m unittest test_codex_sub_agent -k 额度 -v 2>&1 | tail -20
python3 -m unittest test_codex_sub_agent -k 打断 -v 2>&1 | tail -20
```
预期：弯引号那半 FAIL、标点那条 FAIL、brief 负例那条 FAIL（现在会被判成 failed+额度）。

- [ ] **Step 4: 改常量并把判据建到已分类错误行上**

`codex_sub_agent.py` 第 140 行附近：

```python
# 判据**刻意不含 `You've` 那一截**。codex 输出的是弯引号 U+2019，源码里写的是 ASCII
# 直引号——2026-09-22 之前两者对不上，这条特判死了整整一个版本，status 把撞上限的
# 任务全报成「报告缺失或为空」。修法不是把直引号换成弯引号（那仍然押在一个会变的
# 字符上），而是**把判据缩短到不含任何标点的那一段**：剩下全是字母和空格。
USAGE_LIMIT_MARK = "hit your usage limit"
```

在 `runtime_error_lines` **之后**加谓词（它依赖那个函数，位置要在其下方）：

```python
def hit_usage_limit(error_lines):
    """本轮是不是撞上了账号额度上限。

    **收的是 `runtime_error_lines()` 的返回值，不是整轮日志文本。** 这是承重的：
    日志里混着 brief 原文和 codex 转述的子进程输出（本文件 `_ERR_*` 上方那段注释
    已经为同一个理由否掉了裸 grep ERROR）。对整轮文本做子串匹配，一个 brief 里
    提到「hit your usage limit」的任务就会被判成额度问题——而在 `cmd_run` 里，
    那意味着**删掉元数据、换账号重跑整个任务**。判据一旦成为控制依据，
    证据就必须来自**已经分类过的错误行**。

    **`judge` 和 `cmd_run` 共用这一个谓词。** 两处各写一套必然漂移，
    而判据漂移正是本工具反复在修的那类 bug。`cmd_run` 为此要多跑一次
    `runtime_error_lines`（实测 0.83MB 日志约 7.6ms，而 run 是分钟级的）——
    不为省这 8ms 给 `Verdict` 加字段：那要改 7 个构造点，churn 比多一次扫描大。
    """
    return any(USAGE_LIMIT_MARK in line for line in error_lines)
```

`judge()` 里那一行改成（`errors` 在函数开头已经算好）：

```python
        if hit_usage_limit(errors):
```

- [ ] **Step 5: 跑测试确认通过**

```bash
python3 -m unittest test_codex_sub_agent 2>&1 | tail -3
```
预期：全绿。

- [ ] **Step 6: 拿未经我手的真实字节验一次**

```bash
python3 -c "
import codex_sub_agent as ca, glob, pathlib
ok = 0
for f in glob.glob('/home/xy/.codex-subagent*/logs/*.log'):
    t = pathlib.Path(f).read_text(errors='replace')
    if ca.hit_usage_limit(ca.runtime_error_lines(t)): ok += 1
print('真实日志里判出额度上限的文件数:', ok)
assert ok > 0, '现网明明有 27 份带额度错误的日志，一份都判不出来说明判据仍然是坏的'
print('OK')
"
```
预期：打印一个大于 0 的数字和 `OK`。**这一步是 Step 2 的 fixture 替代不了的**——第 8 种空测试形态的成因正是「fixture 经过了我的手」。

- [ ] **Step 7: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "fix: 额度判据既匹配不上真的，又匹配得上假的

匹配不上真的：常量用 ASCII 直引号，codex 输出弯引号 U+2019，27 份日志
54 行全是弯引号，这条特判死了整整一个版本。

匹配得上假的（更严重）：判据对整轮自由文本做裸子串匹配，而本文件 _ERR_*
上方的注释早就为同一个理由否掉了裸 grep ERROR——日志里混着 brief 原文和
codex 转述的子进程输出。一个 brief 里提到这句话的任务会被判成额度问题，
而接下来 cmd_run 会据此删元数据、换账号重跑。判据一旦成为控制依据，
证据就必须来自已分类的错误行。

测试为什么没挡住：fixture 和常量共享同一个未经现实核对的假设。
它不是什么都不测（禁掉匹配会红），而是对「改引号字符」这一类突变毫无反应。
已作为第 8 种空测试形态补进模块头的清单。"
```

---

### Task 2: 从日志里解析恢复时间

**Files:**
- Modify: `codex_sub_agent.py`（`hit_usage_limit` 下面）
- Modify: `test_codex_sub_agent.py`（新建 `TestParseResetTime`）

**Interfaces:**
- Consumes: 无
- Produces: `parse_reset_time(text) -> (datetime.datetime, str) | None`
  **返回元组**：时间和它的原始串必须来自同一次匹配，拆成两个函数就可能各匹配各的、对不上。

- [ ] **Step 1: 写失败的测试**

`datetime` 测试文件顶部还没有，加进 import 块（`contextlib` 之后）。其余（`tempfile`/`json`/`os`/`mock`/`argparse`/`io`）都已有。

```python
class TestParseResetTime(unittest.TestCase):
    """真实消息长这样（2026-09-22 从 24 份会话文件取）：
        You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage
        to purchase more credits or try again at Sep 25th, 2026 5:04 PM.
    """

    def test_解析真实消息(self):
        got = ca.parse_reset_time("or try again at Sep 25th, 2026 5:04 PM.")
        self.assertEqual(got[0], datetime.datetime(2026, 9, 25, 17, 4))
        self.assertEqual(got[1], "Sep 25th, 2026 5:04 PM")

    def test_四种序数后缀都要认(self):
        for day, suffix in ((1, "st"), (2, "nd"), (3, "rd"), (4, "th")):
            with self.subTest(日=day):
                self.assertEqual(
                    ca.parse_reset_time(f"try again at Sep {day}{suffix}, 2026 5:04 PM")[0].day,
                    day)

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

    def test_返回朴素时间_不编造时区(self):
        # 消息里没有时区，实测也推不出来。编一个出来就是撒谎；
        # 这个值只当排序键，朴素时间足够。
        self.assertIsNone(ca.parse_reset_time("try again at Sep 25th, 2026 5:04 PM")[0].tzinfo)
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 -m unittest test_codex_sub_agent.TestParseResetTime -v
```
预期：全部 ERROR，`no attribute 'parse_reset_time'`。

- [ ] **Step 3: 实现**

`codex_sub_agent.py` 顶部**已经有 `import datetime`**（第 9 行），不用加。

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

    **返回朴素本地时间，不编造时区。** 消息里没有时区，实测也推不出来（恢复时间在
    3~4 天后，拿文件 mtime 反推不出来）。这样做安全，是因为这个值**只当排序键**
    （见 `accounts_by_availability`）：误差 ±12h 只在两个账号的恢复时间相差 12h
    以内时才改变顺序，代价是多一次 codex 启动。

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

- [ ] **Step 4: 跑测试确认通过 + 拿真实日志验**

```bash
python3 -m unittest test_codex_sub_agent.TestParseResetTime -v
python3 -c "
import codex_sub_agent as ca, glob, pathlib
for f in glob.glob('/home/xy/.codex-subagent*/logs/*.log'):
    t = pathlib.Path(f).read_text(errors='replace')
    if 'usage limit' in t:
        print(f.split('/')[-1], ca.parse_reset_time(t)); break
"
```
预期：7 条 PASS；真实日志解析出 `(datetime.datetime(2026, 9, 26, 17, 47), 'Sep 26th, 2026 5:47 PM')` 这样的东西。

- [ ] **Step 5: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "feat: 解析额度上限消息里的恢复时间

返回 (时间, 原始串) 元组：两样必须来自同一次匹配，拆开就会对不上。
朴素本地时间不编造时区——消息里没有，实测也推不出来；只当排序键，误差有界。"
```

---

### Task 3: `usage_limit.json` 的读写

**Files:**
- Modify: `codex_sub_agent.py`（`parse_reset_time` 下面）
- Modify: `test_codex_sub_agent.py`（新建 `TestUsageLimitFile`）

**Interfaces:**
- Consumes: 无
- Produces: `usage_limit_path(home)`、`write_usage_limit(home, reset_at, raw)`、`read_usage_limit(home) -> datetime | None`

**背景：** 这个文件和 `tasks/<任务>.json` 有一个关键区别：**它是账号级、跨任务共享的**。同一个账号上的两个任务可能同时撞上限、同时写。`write_meta` 用固定临时名是安全的，因为那里一个任务只可能有一个写者——**这个前提在这里不成立**。

- [ ] **Step 1: 写失败的测试**

```python
class TestUsageLimitFile(unittest.TestCase):
    def setUp(self):
        # 纯文件操作，不碰 HOME——`_HomeSandbox` 的 docstring 说了「纯函数测试不要它」
        self.home = pathlib.Path(tempfile.mkdtemp())

    def test_写了能读回来(self):
        when = datetime.datetime(2026, 9, 25, 17, 4)
        ca.write_usage_limit(self.home, when, "Sep 25th, 2026 5:04 PM")
        self.assertEqual(ca.read_usage_limit(self.home), when)

    def test_键只有两个(self):
        # seen_at 之类没有消费者的字段一旦加进去就再也删不掉了
        ca.write_usage_limit(self.home, datetime.datetime(2026, 9, 25, 17, 4), "raw")
        self.assertEqual(set(json.loads(ca.usage_limit_path(self.home).read_text())),
                         {"reset_at", "raw"})

    def test_raw原样保留(self):
        # raw 是审计线索：解析错了要能一眼看出错在哪，所以不许加工
        ca.write_usage_limit(self.home, datetime.datetime(2026, 9, 25, 17, 4),
                             "Sep 25th, 2026 5:04 PM")
        self.assertEqual(json.loads(ca.usage_limit_path(self.home).read_text())["raw"],
                         "Sep 25th, 2026 5:04 PM")

    def test_四种读失败都退化成无记录(self):
        # 读不出来就当「没有记录」＝排最前＝照样会被试到，那是安全的一边。
        # 反过来（读不出来就认定还在限流）会把可用账号锁死且调用方看不出原因。
        q = ca.usage_limit_path(self.home)
        for name, content in (("文件不存在", None),
                              ("坏 json", "{不是 json"),
                              ("缺键", '{"raw": "x"}'),
                              ("时间串坏了", '{"reset_at": "昨天", "raw": "x"}')):
            with self.subTest(情形=name):
                if content is None:
                    q.unlink(missing_ok=True)
                else:
                    q.write_text(content)
                self.assertIsNone(ca.read_usage_limit(self.home))

    def test_临时文件名带pid_两个并发写者不会互相污染(self):
        # **这条钉的是本文件和 write_meta 的关键区别。** usage_limit.json 是账号级的，
        # 同一账号上的两个任务可能同时撞上限。固定临时名会让两个写者把同一个临时
        # 文件写成混合内容，再原子替换进去。
        a = ca._usage_limit_tmp(self.home, 111)
        b = ca._usage_limit_tmp(self.home, 222)
        self.assertNotEqual(a, b, "两个进程必须拿到不同的临时文件名")
        self.assertTrue(a.name.endswith(".tmp"))

    def test_原子替换_替换前目标仍是完整旧内容(self):
        ca.write_usage_limit(self.home, datetime.datetime(2026, 9, 25, 17, 4), "旧")
        seen = []
        real_replace = os.replace
        def spy(src, dst):
            seen.append(json.loads(pathlib.Path(dst).read_text())["raw"])
            return real_replace(src, dst)
        # patch 模块自己的 os，别 patch 全局的——全局的会波及 unittest 内部
        with mock.patch.object(ca.os, "replace", spy):
            ca.write_usage_limit(self.home, datetime.datetime(2026, 9, 26, 17, 47), "新")
        self.assertEqual(seen, ["旧"], "替换发生前目标文件必须还是完整的旧内容")
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 -m unittest test_codex_sub_agent.TestUsageLimitFile -v
```
预期：全部 ERROR。

- [ ] **Step 3: 实现**

```python
USAGE_LIMIT_FILE = "usage_limit.json"


def usage_limit_path(home):
    """限流记录放隔离目录根部。

    限流是**账号**的属性，而账号已经有一个家，不必为它新开一个状态目录。
    `ensure_isolation` 的不变量是「五个子目录存在、共享扫描根为空、config.toml
    不是软链、auth.json 软链到该账号」——**它不枚举也不拒绝顶层多余文件**
    （2026-09-22 实测：放一个文件进去再跑一次，不拒且文件保留）。
    """
    return home / USAGE_LIMIT_FILE


def _usage_limit_tmp(home, pid):
    """写限流记录用的临时文件名。**带 pid，不能用固定名。**

    和 `write_meta` 的关键区别：那份是**任务级**的，一个任务只可能有一个写者
    （`cmd_run`/`cmd_resume` 都先拒绝「还在跑」的同名任务），所以固定名安全。
    这份是**账号级、跨任务共享**的——同一个账号上的两个任务可以同时撞上限、
    同时写。固定名会让两个写者把同一个临时文件写成混合内容，
    然后原子替换把这份混合内容装进去，`read_usage_limit` 读出 `None`。
    """
    q = usage_limit_path(home)
    return q.with_name(f"{q.name}.{pid}.tmp")


def write_usage_limit(home, reset_at, raw):
    """记下这个账号什么时候恢复。**只在 `parse_reset_time` 成功时调用。**

    `raw` 是审计线索：解析错了（时区、OpenAI 改文案）一眼看得出，不用翻日志。
    **没有 `seen_at`**——唯一的读者是排序，排序只看 `reset_at`；
    没有消费者的字段一旦加进去就再也删不掉了。
    """
    tmp = _usage_limit_tmp(home, os.getpid())
    tmp.write_text(json.dumps({"reset_at": reset_at.isoformat(), "raw": raw},
                              ensure_ascii=False, indent=2))
    os.replace(tmp, usage_limit_path(home))


def read_usage_limit(home):
    """这个账号预计什么时候恢复；没记录或记录坏了都返回 `None`。

    **坏了返回 `None` 是刻意的**：调用方把 `None` 当成「无记录＝排最前＝照样会被
    试到」，那是安全的一边。反过来（读不出来就认定它还在限流）会把一个可用账号
    锁死，而调用方看不出原因——那正是本工具反复在修的那类谎。
    """
    try:
        got = json.loads(usage_limit_path(home).read_text())
        return datetime.datetime.fromisoformat(got["reset_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
```

- [ ] **Step 4: 跑测试并确认没破坏隔离目录不变量**

```bash
python3 -m unittest test_codex_sub_agent.TestUsageLimitFile -v
python3 -c "
import codex_sub_agent as ca, datetime
home = ca.ensure_isolation('acct2')
ca.write_usage_limit(home, datetime.datetime(2026,1,1,0,0), 'smoke test')
ca.ensure_isolation('acct2')          # 多了个文件之后再跑一次，不许拒
print('ensure_isolation 不介意多余文件：OK')
ca.usage_limit_path(home).unlink()    # 清掉冒烟测试留下的假记录
"
```
预期：全 PASS + 打印 OK。**最后那行 unlink 不能漏**，否则会给 acct2 留一条假的限流记录。

- [ ] **Step 5: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "feat: usage_limit.json —— 每个隔离目录记一条恢复时间

临时文件名带 pid：这份是账号级、跨任务共享的，write_meta 那份是任务级的，
后者「一个任务只有一个写者」的前提在这里不成立，照搬固定名会让两个并发
写者把同一个临时文件写成混合内容再原子替换进去。

只有 reset_at 和 raw 两个键，没有 seen_at（没有消费者）。
读失败一律 None，退化成「无记录＝排最前＝照样会被试到」。"
```

---

### Task 4: 保留名护栏 + 按可用性排序

**Files:**
- Modify: `codex_sub_agent.py`（`account_choices()` 内部、其下方新增函数）
- Modify: `test_codex_sub_agent.py`（新建 `TestAccountChoicesGuards`、`TestAccountOrder`）

**Interfaces:**
- Consumes: `account_choices`、`isolation_home`、`find_meta`、`read_usage_limit`、`auth_source`
- Produces: `AUTO = "auto"`、`RESERVED_ACCOUNT_NAMES`、`accounts_by_availability(task) -> list[str]`

**背景（实测，不是设想）：** 在 `~/.codex-accounts/` 下建 `default` 和 `auto` 两个目录，现有 `account_choices()` 返回 `['default', 'acct2', 'auto', 'default']`——**`default` 出现两次，且两次 `isolation_home()` 指向同一个 home**。后果：同一个 home 被扫两遍，`find_meta` 对同一份元数据数出两份，当场拒绝「任务名在多个隔离目录里都有」。而 `auto` 一旦成为 CLI 模式词，名叫 `auto` 的真账号就再也没法被明确指定。

- [ ] **Step 1: 写保留名的失败测试**

```python
class TestAccountChoicesGuards(_HomeSandbox):
    def test_保留名目录要拒跑并点名(self):
        # default：现有代码会返回两个 'default'，两次指向同一个 home，
        #          find_meta 会对同一份元数据数出两份，当场误拒。
        # auto   ：它是 --account 的模式词，真账号同名就再也没法被明确指定。
        for name in ("default", "auto"):
            with self.subTest(保留名=name):
                d = self.home / ".codex-accounts" / name
                d.mkdir(parents=True)
                with self.assertRaises(ca.Rejected) as got:
                    ca.account_choices()
                self.assertIn(name, got.exception.message)
                d.rmdir()

    def test_候选里没有重复(self):
        for name in ("acct2", "acct3"):
            (self.home / ".codex-accounts" / name).mkdir(parents=True)
        got = ca.account_choices()
        self.assertEqual(len(got), len(set(got)))
```

- [ ] **Step 2: 实现保留名护栏**

在 `codex_sub_agent.py` 里 `account_choices` 上方：

```python
AUTO = "auto"
# `default` 是主账号的固定名（不对应 ~/.codex-accounts 下的目录），
# `auto` 是 `--account` 的模式词。两个都不许被真账号目录占用。
RESERVED_ACCOUNT_NAMES = ("default", AUTO)
```

在 `account_choices()` 的 `for name in extra:` 循环里、坏字符检查**之后**加：

```python
        if name in RESERVED_ACCOUNT_NAMES:
            reject(f"账号目录 {accounts_dir / name} 占用了保留名 {name!r}。\n"
                   f"`default` 是主账号的固定名，`{AUTO}` 是 --account 的模式词。\n"
                   f"实测后果：目录叫 default 时候选里会出现两个 default、"
                   f"两次指向同一个隔离目录，find_meta 会对同一份元数据数出两份并误拒；"
                   f"目录叫 {AUTO} 时这个账号再也没法被明确指定。\n"
                   f"把这个目录改个名。")
```

**拒跑而不是静默去重**：静默去重会让住在那个目录里的任务从 `status` 里消失，正是这个函数的
注释反复强调要避免的静默失效。

- [ ] **Step 3: 写排序的失败测试**

```python
class TestAccountOrder(_HomeSandbox):
    """排序只影响**先试谁**，不影响**试不试**。所以这里每条断言都是关于顺序的，
    只有最后一条是关于「一个都不少」——那是本设计的核心约束。
    """

    def setUp(self):
        super().setUp()      # HOME 沙箱：Path.home() 和 $HOME 两条都 patch 掉
        for name in ("acct2", "acct3"):
            d = self.home / ".codex-accounts" / name
            d.mkdir(parents=True)
            (d / "auth.json").write_text("{}")     # 有登录态才进候选
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

    def test_过期记录排在无记录之后_但语义仍然正确(self):
        # **这条钉的是一个曾经写错的理由。** 排序不读当前时间，所以过期记录
        # 不会「自动变成无记录」——它排进「恢复得早的那一批」，批内按先后排。
        # 机制是对的，第一版 spec 说的「时间一过自然排到最前面」是错的。
        self._limit("default", datetime.datetime(2000, 1, 1))   # 早就过期了
        self._limit("acct3", datetime.datetime(2099, 1, 1))
        got = ca.accounts_by_availability("t")
        self.assertEqual(got, ["acct2", "default", "acct3"],
                         "无记录的 acct2 仍排第一，过期的 default 排第二")

    def test_没有登录态的账号不进候选(self):
        # ensure_isolation 会因为缺登录态直接 reject 退出 2。不过滤的话，
        # 一个账号缺登录态就会把整条 auto 命令打死，哪怕别的账号完全可用。
        (self.home / ".codex-accounts" / "acct3" / "auth.json").unlink()
        self.assertNotIn("acct3", ca.accounts_by_availability("t"))

    def test_有登录态的一个都不少(self):
        # **本设计的核心约束。** 额度记录只排顺序，绝不把账号排除出候选。
        for account in ("default", "acct2", "acct3"):
            self._limit(account, datetime.datetime(2099, 1, 1))
        self.assertEqual(sorted(ca.accounts_by_availability("t")),
                         sorted(ca.account_choices()))
```

- [ ] **Step 4: 跑测试确认它失败**

```bash
python3 -m unittest test_codex_sub_agent.TestAccountChoicesGuards test_codex_sub_agent.TestAccountOrder -v
```
预期：保留名那两条 FAIL（现在不拒），排序那些 ERROR（没有 `accounts_by_availability`）。

- [ ] **Step 5: 实现排序**

```python
def accounts_by_availability(task):
    """按「预计什么时候能用」给候选账号排序。

    **额度记录只排顺序，不准拒绝**——本设计最重要的一条约束。
    「全部账号都撞上额度上限」这个结论必须来自**真实尝试**（见 `cmd_run` 的循环），
    不能来自这里读到的文件。文件会错（时区、时钟偏移、OpenAI 改文案），
    只排顺序的话后果被限制在「顺序排差、多几次启动」；一旦允许它拒绝，
    同一个错就变成「把好账号误判成死的、还告诉调用方没号可用」。

    **登录态是唯一的例外，而且它不违反上面那条。** 那条约束管的是**额度记录**
    ——一份会过期、会出错的缓存。登录态是当场可查的硬前提：没有 `auth.json`，
    `ensure_isolation` 会直接 `reject` 退出 2，于是**一个账号缺登录态就会把整条
    auto 命令打死**，哪怕别的账号完全可用。所以在这里就把它们滤掉。

    排序键三段：
    ① 这个任务是不是已经住在该账号——住着的排最前（沿用它的家），
       但仍可被 `cmd_run` 的重试搬走
    ② 恢复时间；没记录就是 `datetime.min`，排最前，照样会被试到。
       **过期记录不会自动变成「无记录」**：本函数不读当前时间，一条过期记录
       永远排在无记录之后。这不是 bug——它排进「恢复得早的那一批」，
       批内按先后排，语义依然正确，而清理需要读时钟、需要定义「多久算过期」，
       是纯增实体
    ③ 账号名——**第二键必须有**，否则顺序跟着 `account_choices()` 的扫描顺序漂，
       同一个输入在两台机器上给出两种结果
    """
    own_home, _ = find_meta(task)
    def sort_key(account):
        home = isolation_home(account)
        return (0 if home == own_home else 1,
                read_usage_limit(home) or datetime.datetime.min,
                account)
    return sorted((a for a in account_choices() if auth_source(a).exists()), key=sort_key)
```

- [ ] **Step 6: 跑测试确认通过**

```bash
python3 -m unittest test_codex_sub_agent.TestAccountChoicesGuards test_codex_sub_agent.TestAccountOrder -v
python3 -m unittest test_codex_sub_agent 2>&1 | tail -3
```
预期：新类全 PASS，全量全绿。

- [ ] **Step 7: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "feat: 保留名护栏 + 按恢复时间给账号排序

保留名是实测出来的：~/.codex-accounts/ 下建 default 目录，候选会返回两个
default 且两次指向同一个 home，find_meta 对同一份元数据数出两份当场误拒。
拒跑而不是静默去重——静默去重会让住在那个目录里的任务从 status 消失。

排序只排顺序不准拒绝。唯一的例外是登录态：它是当场可查的硬前提，
不过滤的话一个账号缺 auth.json 就会把整条 auto 命令打死。

过期记录不会自动变成无记录（本函数不读当前时间）。机制保留，
第一版 spec 说的「时间一过自然排到最前面」是错的，已改正并加测试钉住。"
```

---

### Task 5: `--account auto` 与撞上限换号重试

**Files:**
- Modify: `codex_sub_agent.py` —— `cmd_run()` 重写、`build_parser()` 里 run 的 `--account`
- Modify: `test_codex_sub_agent.py`（新建 `TestRunRetry`）

**Interfaces:**
- Consumes: 前四个 Task 的全部产物
- Produces: 无新公开函数

**背景（实现者必读三条）：**

1. `run_codex` 在 spawn **之前**就把元数据写进该账号的 home（1311 行写、1354 行才 `Popen`）。而 `cmd_run` 有一道护栏：同名任务不许跨账号。不删旧元数据，第二次尝试会被自己的护栏挡住。
2. **但最后一个候选失败时不删。** 审查实测过后果：三个账号全满之后 `status` 列表为空、`status <任务名>` 说「没有这个任务」——任务凭空消失。
3. **「任务还在跑」的入口检查不能替代 `run_codex` 内部每次 spawn 前的等待。** 前者问「这个任务名此刻有没有活着的 codex」，查一次就够；后者问「上一轮的 writer 和 codex 是否都排干了」，**每次 spawn 都必须做**，不许因为刚删过元数据就跳过。本 Task 不碰 `run_codex`，保持原样即可。

- [ ] **Step 1: 写失败的测试**

```python
class TestRunRetry(_HomeSandbox):
    """全部用假的 `run_codex` —— 真跑 codex 既慢又要花钱，而这里要测的是
    **循环的控制流**，不是 codex 本身。
    """

    LIMIT_2 = ("\x1b[1m\x1b[31mERROR:\x1b[0m\x1b[0m You’ve hit your usage limit. "
               "Visit … or try again at Sep 25th, 2026 5:04 PM.")
    LIMIT_3 = ("\x1b[1m\x1b[31mERROR:\x1b[0m\x1b[0m You’ve hit your usage limit. "
               "Visit … or try again at Sep 26th, 2026 5:47 PM.")
    BROKEN = "\x1b[1m\x1b[31mERROR:\x1b[0m\x1b[0m something broke"
    FINE = "一切正常\n"

    def setUp(self):
        super().setUp()      # 基类已经建好 ~/.codex/auth.json（default 的登录态）
        for name in ("acct2", "acct3"):
            d = self.home / ".codex-accounts" / name
            d.mkdir(parents=True)
            (d / "auth.json").write_text("{}")
        (self.home / "work").mkdir()
        self.brief = self.home / "brief.md"
        self.brief.write_text("干活\n")
        self.tried = []
        self.briefs = []

    def _args(self, account):
        return argparse.Namespace(task="t", dir=str(self.home / "work"),
                                  brief=str(self.brief), effort="high",
                                  account=account, skills=[])

    def _fake_run(self, outcomes):
        """outcomes: 账号 → 本轮日志文本。"""
        def fake(kind, home, task, meta, make_argv):
            self.tried.append(meta["account"])
            ca.write_meta(home, task, meta)          # 真 run_codex 也是先写元数据
            report = ca._report_path(home, task)
            report.parent.mkdir(parents=True, exist_ok=True)
            argv = make_argv(str(report))
            self.briefs.append(argv[-1])             # 记下每轮真正发出去的 prompt
            text = outcomes[meta["account"]]
            if "usage limit" not in text:
                report.write_text("干完了\n")
            return ca.Round(report, text)
        return fake

    def _run(self, account, outcomes):
        with mock.patch.object(ca, "run_codex", self._fake_run(outcomes)), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = ca.cmd_run(self._args(account))
        return code, out.getvalue()

    def test_第一个撞上限就换下一个(self):
        code, _ = self._run(ca.AUTO, {"acct2": self.LIMIT_2, "acct3": self.FINE,
                                      "default": self.FINE})
        self.assertEqual(self.tried[:2], ["acct2", "acct3"])
        self.assertEqual(code, ca.EXIT["success"])

    def test_每轮的prompt逐字不变(self):
        # 只有账号变，brief/effort/skills 一个字都不许变
        self._run(ca.AUTO, {"acct2": self.LIMIT_2, "acct3": self.FINE, "default": self.FINE})
        self.assertEqual(len(set(self.briefs)), 1, "两轮发出去的 prompt 必须逐字相同")

    def test_换号前把上一个账号的元数据删掉(self):
        # 不删的话第二次尝试会被 cmd_run 自己的跨账号同名护栏挡住
        self._run(ca.AUTO, {"acct2": self.LIMIT_2, "acct3": self.FINE, "default": self.FINE})
        self.assertFalse(ca.meta_path(ca.isolation_home("acct2"), "t").exists())
        self.assertTrue(ca.meta_path(ca.isolation_home("acct3"), "t").exists())

    def test_撞上限要把恢复时间记下来(self):
        self._run(ca.AUTO, {"acct2": self.LIMIT_2, "acct3": self.FINE, "default": self.FINE})
        self.assertEqual(ca.read_usage_limit(ca.isolation_home("acct2")),
                         datetime.datetime(2026, 9, 25, 17, 4))

    def test_全撞上限_退出1且逐个列出恢复时间(self):
        code, printed = self._run(ca.AUTO, {"acct2": self.LIMIT_2, "acct3": self.LIMIT_3,
                                            "default": self.LIMIT_3})
        self.assertEqual(code, ca.EXIT["failed"])
        for account in ("acct2", "acct3", "default"):
            self.assertIn(account, printed)
        self.assertIn("09-25", printed)

    def test_全撞上限之后status仍然查得到这个任务(self):
        # **最后一个候选不许删元数据。** 删了的话任务凭空消失：
        # status 列表为空、status <任务名> 说「没有这个任务」。
        self._run(ca.AUTO, {"acct2": self.LIMIT_2, "acct3": self.LIMIT_3,
                            "default": self.LIMIT_3})
        home, meta = ca.find_meta("t")
        self.assertIsNotNone(meta, "全满之后任务必须还查得到")

    def test_尝试次数上界是候选数_每个最多一次(self):
        self._run(ca.AUTO, {"acct2": self.LIMIT_2, "acct3": self.LIMIT_3,
                            "default": self.LIMIT_3})
        self.assertEqual(sorted(self.tried), sorted(ca.accounts_by_availability("t")))
        self.assertEqual(len(self.tried), len(set(self.tried)), "每个候选最多启动一次")

    def test_不是额度问题就立刻停(self):
        # 换号救不了「代码写错了」，只会白烧一轮。代价是后面那个能用的账号
        # 这一次不会被试到——这是刻意选的，见 spec。
        self._run(ca.AUTO, {"acct2": self.BROKEN, "acct3": self.FINE, "default": self.FINE})
        self.assertEqual(self.tried, ["acct2"])

    def test_指定账号撞上限_记录但不重试(self):
        code, _ = self._run("acct2", {"acct2": self.LIMIT_2, "acct3": self.FINE,
                                      "default": self.FINE})
        self.assertEqual(self.tried, ["acct2"], "强制模式不许换号")
        self.assertEqual(code, ca.EXIT["failed"])
        # 但事实照记：强制模式踩到的坑要让 auto 模式变聪明
        self.assertEqual(ca.read_usage_limit(ca.isolation_home("acct2")),
                         datetime.datetime(2026, 9, 25, 17, 4))

    def test_一个任务只住一个隔离目录(self):
        self._run(ca.AUTO, {"acct2": self.LIMIT_2, "acct3": self.FINE, "default": self.FINE})
        self.assertEqual(len(ca.all_metas()), 1)

    def test_所有账号都没登录态_拒跑并点名(self):
        for q in self.home.glob(".codex-accounts/*/auth.json"):
            q.unlink()
        (self.home / ".codex" / "auth.json").unlink()
        with self.assertRaises(ca.Rejected) as got:
            ca.cmd_run(self._args(ca.AUTO))
        self.assertIn("auth.json", got.exception.message)
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 -m unittest test_codex_sub_agent.TestRunRetry -v
```
预期：全部 FAIL/ERROR。

- [ ] **Step 3: 改 parser**

```python
    r.add_argument("--account", required=True, choices=account_choices() + [AUTO],
                   help=f"codex 账号；{AUTO} = 自动挑一个没在限流的，撞上额度上限就换下一个")
```

`--account` **仍然必填**：没有任何隐式选中的值。

- [ ] **Step 4: 重写 `cmd_run`**

前面的 `--dir` / `--brief` 校验**一行不改**。从 `home = isolation_home(args.account)` 那行起换成：

```python
    old_home, old_meta = find_meta(args.task)
    if old_meta is not None:
        # 「还在跑」只查一次，进循环之前：它问的是「这个任务名此刻有没有活着的
        # codex」，和试哪个账号无关。**这不替代 run_codex 内部每次 spawn 前的等待**
        # ——那一条问的是「上一轮的 writer 和 codex 排干了没有」，每轮都必须做。
        if find_codex_pid(_report_path(old_home, args.task)) is not None:
            reject(f"任务名 {args.task} 还在跑，换个名字或先 `codex-sub-agent stop {args.task}`")
        print(f"[codex-sub-agent] 提示：任务名 {args.task} 复用，上一轮的报告会被删掉、日志会被追加")

    if args.account == AUTO:
        candidates = accounts_by_availability(args.task)
        if not candidates:
            reject("没有任何账号有登录态，auto 模式无从分配：\n"
                   + "\n".join(f"  {a} 缺 {auth_source(a)}" for a in account_choices())
                   + "\n先跑 `codex-acct login <账号>`。")
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
        # 谓词收的是**已分类的错误行**，不是整轮文本：brief 原文和子进程输出
        # 都进不了这个集合。判据一旦成为「删状态、换账号重跑」的依据，
        # 证据就必须来自已分类的事实（见 hit_usage_limit）。
        limited = verdict.state == "failed" and hit_usage_limit(runtime_error_lines(round.text))
        if limited:
            # 从**同一个 round.text** 解析，绝不另读整份历史日志——那会把上一轮、
            # 上一个任务的旧额度错误和旧时间当成本轮事实。
            # 解析不出时间也要记进 exhausted（值为 None）：这个账号确实满了，
            # 漏记会让「全撞上限」那条汇总少一行。
            hit = parse_reset_time(round.text)
            if hit is not None:
                write_usage_limit(home, *hit)
            exhausted.append((account, hit[0] if hit else None))
            if args.account == AUTO and attempt + 1 < len(candidates):
                # run_codex 在 spawn 前就把元数据写进了这个 home。不删的话下一个
                # 账号会被跨账号同名护栏挡住，而且这一份会变成够不着的孤儿。
                # **最后一个候选不走这条路**：删了的话全满之后任务凭空消失，
                # status 列表为空、status <任务名> 说「没有这个任务」。
                meta_path(home, args.task).unlink(missing_ok=True)
                when = f"，恢复于 {hit[0]:%m-%d %H:%M}" if hit else ""
                print(f"[codex-sub-agent] {account} 撞上额度上限{when}，换下一个账号")
                continue
        if len(exhausted) > 1:
            # 不新增状态，只把 reason 说清楚——沿用 judge 的规矩。
            verdict = Verdict("failed", f"全部 {len(exhausted)} 个账号都撞上额度上限",
                              [f"{a:<8} " + (f"恢复于 {t:%m-%d %H:%M}" if t else "恢复时间没解析出来")
                               for a, t in exhausted])
        _print_verdict(args.task, verdict)
        print(f"  报告 {_report_path(home, args.task)}\n  日志 {_log_path(home, args.task)}")
        return EXIT[verdict.state]
```

`ensure_isolation(account)` 返回的就是 `home`（末行 `return d`），不必再调 `isolation_home`。

- [ ] **Step 5: 跑测试确认通过**

```bash
python3 -m unittest test_codex_sub_agent.TestRunRetry -v
python3 -m unittest test_codex_sub_agent 2>&1 | tail -3
```
预期：新类 11 条全 PASS，全量全绿。

- [ ] **Step 6: 真跑一次冒烟**

```bash
cd /home/xy/.claude/skills/codex-sub-agent
printf '回答一个字：好\n' > /tmp/smoke-brief.md
./codex_sub_agent.py run --task smoke-auto --dir /tmp \
  --brief /tmp/smoke-brief.md --effort low --account auto --no-skill
echo "退出码 $?"
```
预期：最终 `success`，退出码 0。**当前 `default` 和 `acct3` 都在限流中（恢复时间 9/26 17:47 和 9/25 17:04），所以 auto 应该选中 `acct2`**——如果它先试了别的，说明排序没生效。

- [ ] **Step 7: 提交**

```bash
git add codex_sub_agent.py test_codex_sub_agent.py
git commit -m "feat: --account auto —— 自动挑账号，撞上额度上限就换下一个

重试前删掉上一个账号的元数据：run_codex 在 spawn 前就写了它，不删的话
第二次尝试被跨账号同名护栏挡住。但最后一个候选不删——删了的话全满之后
任务凭空消失，status 列表为空、status <任务名> 说「没有这个任务」。

auto 模式预先滤掉没有登录态的账号：ensure_isolation 会因此 reject 退出 2，
不滤的话一个账号缺 auth.json 就把整条命令打死，哪怕别的账号完全可用。

额度谓词收的是已分类的错误行，不是整轮文本。恢复时间从 judge 用的那同一个
Round.text 解析，不另读历史日志。

非额度失败立即停止：换号救不了代码写错，只会白烧一轮。"
```

---

### Task 6: 文档同步

**Files:** `SKILL.md`（账号那一行）、`README.md`

**注意：** `SKILL.md` 工作区里有一处**未提交**的改动，补的是「一个账号可以同时开多个子代理」，改的正是要重写的同一行。**把这句话并进新版本，不要单独提交、也不要丢掉。**

- [ ] **Step 1: 改 `SKILL.md` 账号那一行**

```
| 账号 | `--account auto` —— **默认就写这个**，工具自己挑一个没在限流的，撞上额度上限会自动换下一个重试，全部满了才报错并告诉你每个账号什么时候恢复。**一个账号可以同时开多个子代理**，不用排队等前一个跑完。要盯某一个账号就写它的名字（`default` / `acct2` / `acct3`，由 `~/.codex-accounts/` 扫出来，加一个账号就自动认），那样撞上限**不会**换号。每个账号有各自独立的隔离目录，互不干扰 |
```

- [ ] **Step 2: 改 `README.md`**

```bash
grep -n "account" README.md
```
把示例里的 `--account <账号>` 改成 `--account auto`，参数说明加一行：

```
| `--account` | `auto` 自动挑（撞额度上限自动换号重试），或写死某个账号名 |
```

- [ ] **Step 3: 核对文档和 CLI 一致**

```bash
./codex_sub_agent.py run --help | grep -A2 -- --account
grep -n "auto" SKILL.md README.md
```

- [ ] **Step 4: 提交**

```bash
git add SKILL.md README.md
git commit -m "docs: --account auto

并入工作区里那处未提交的改动（一个账号可以同时开多个子代理），它改的是同一行。"
```

---

## Self-Review 结果

**Spec 覆盖**（逐条核对 spec v2 的验收判据）：

| spec 验收判据 | 哪个 Task |
|---|---|
| 1 额度正例：直引号、弯引号、真实日志字节 | Task 1 Step 2 + Step 6 |
| 1 额度负例：brief 提到但实为打断 → interrupted、不换号不删元数据 | Task 1 Step 2 第三条 + Task 5 的 `test_不是额度问题就立刻停` |
| 1 额度负例的负例：判据过宽时负例必须变红 | Task 1 Step 2 第四条 |
| 2 恢复时间的七种情形 + 朴素时间 | Task 2 Step 1 |
| 3 缓存四种读失败 / 临时名带 pid / 原子替换 | Task 3 Step 1 |
| 4 第一个满第二个成、顺序、prompt 不变、退出码 | Task 5 前四条 |
| 4 每个候选最多一次、强制模式一次 | Task 5 `test_尝试次数上界…`、`test_指定账号…` |
| 4 非额度失败立即停止 | Task 5 `test_不是额度问题就立刻停` |
| 4 全满退出 1 且 status 仍查得到、恢复时间未知如实显示 | Task 5 两条 + `Verdict` 的 detail 分支 |
| 4 缺登录态被跳过；一个都没有时 reject 并点名 | Task 4 `test_没有登录态的账号不进候选` + Task 5 `test_所有账号都没登录态…` |
| 5 保留名 default/auto 拒跑并点名；候选无重复 | Task 4 Step 1 |
| 6 既有等待/轮次边界/信号测试保留全绿 | 每个 Task 的 Step「全量全绿」；本计划不碰 `run_codex` |
| 7 SKILL.md / README.md 一致 | Task 6 |
| 7 内化后删除 spec 与计划 | **本计划之外**，由工作流程第 8 步完成（已登记在 xy-goal 契约 T8） |

**无遗漏。**

**占位符扫描：** 无 TBD/TODO；每个代码步骤都有可直接粘贴的真代码；每条测试都有真断言。

**类型一致性：**
- `hit_usage_limit(error_lines)` 收**列表**（Task 1 定义），Task 5 传 `runtime_error_lines(round.text)` —— 一致
- `parse_reset_time` 返回 `(datetime, str) | None`（Task 2），Task 5 用 `hit[0]` 和 `write_usage_limit(home, *hit)` —— 一致
- `accounts_by_availability(task)` 收任务名（Task 4），Task 5 传 `args.task` —— 一致
- `ensure_isolation(account)` 返回 `home`（源码末行 `return d`），Task 5 直接用返回值 —— 一致
- `_usage_limit_tmp(home, pid)` 在 Task 3 定义并被同 Task 的测试直接调用 —— 一致
