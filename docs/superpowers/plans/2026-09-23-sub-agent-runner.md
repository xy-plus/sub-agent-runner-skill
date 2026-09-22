# sub-agent-runner Implementation Plan (v3)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans。步骤用 `- [ ]` 跟踪。

**Goal:** 同一个工具既能派 codex 子代理也能派 DeepSeek 子代理，共用同一套任务名、判据、
报告和打断/续跑协议；并把工具改名成 `sub-agent-runner`。

**Architecture:** `judge` **一行不改**。runner 的职责是把「本轮的产物」摆成 `judge` 已经
认识的形状——报告文件 + 本轮日志文本。

配套 spec：`docs/superpowers/specs/2026-09-23-sub-agent-runner-design.md`（v3，290 行）。
**每条决定的理由都在 spec 里，全部依据是 2026-09-23 的实测。说不清楚就去读它。**

---

## v3 为什么重写：plan v2 是一份「全绿但没接上线」的计划

plan v2 定义了五个新函数、写了 31 条测试，**全绿**——审查（DeepSeek 子代理，66 轮）
照着它搭了一份参考实现并跑了 30 个突变，结论是那五个函数
（`deepseek_env`／`finish_deepseek_round`／`deepseek_log_lines`／`log_line_filter`／
`split_complete_lines`）**一个调用点都没有**：

> 一个「deepseek runner 永不写报告、永不滤日志、**完全没有隔离**」的实现，
> 照样能让计划里那 31 条新测试全绿。

**教训写进本计划的结构里**：凡是新增一个函数的步骤，同一个步骤必须同时给出
**它的调用点**和**一条端到端断言**（假 `Popen` 驱动真实 `run_codex` → 断言日志里真有那行、
报告真出现/真不出现）。只断言「这个函数被调用时返回什么」的步骤一律不算完成。

---

## Global Constraints

- **`judge` 一行不改。** 想给它加 runner 分支＝设计跑偏了，停下来报告
- **codex 那条路径行为一字不变**，包括 `_tee_until_exit` 的分块读
- **盘上契约一个都不许改**：`~/.codex-subagent*` 目录名、`ROUND_MARK`、`INTERRUPT_MARK` 的文本
- `--runner codex|deepseek` 必填，不给缺省；代码里不许有「读不到 runner 就当 codex」的路径
- deepseek 的 session id 由工具铸造，**必须是独立 argv 元素**，不许 `=` 连接
- **测试一次都不许真起 codex 或 claude**（Task 3 Step 0 有护栏）
- 需要 HOME 沙箱的测试类继承 `_HomeSandbox`；patch 写 `mock.patch`；临时目录不加 rmtree
- 不设测试数量门槛；不新增第三方依赖
- 提交不传 `-c user.email` / `-c user.name`

### 机械改动的纪律（每个 Task 都适用）

改签名会牵动大量既有测试。**只改签名，断言内容一个字不动**——断言两边一起动就是
本仓踩过五次的那种空测试。每次机械改动之后先跑全量，红的只能是「签名对不上」那一类。

实测过的量（改动前，`(?<![\w.])名字\(` 计数，不含注释）：

| 名字 | 模块 | 测试 | 说明 |
|---|---|---|---|
| `ensure_isolation` | 2 | 66 | Task 2 已做 |
| `isolation_home` | 7 | 25 | Task 2 已做 |
| `new_meta` | 5 | 10 | Task 2 已做 |
| `_full_meta`（测试里的构造器） | — | 70 | Task 2 已做：**字典体里加 `runner` 键**，不加则 `_load_meta` 全拒 |
| `find_codex_pid` | 7 | 12 | Task 3 要做。**另有 44 处 `mock.patch.object(ca, "find_codex_pid", …)` 字符串**，名字改了 mock 会 `AttributeError` 当场红，不会静默 |

---

### Task 1: 迁移现存 260 份元数据 ✅ 已完成（无提交，这一步没有代码改动）

- [x] 量现状并确认没有活任务：**260 份**（`~/.codex-subagent` 49、`-acct2` 187、`-acct3` 24），无一在跑
- [x] 用 `write_meta`（原子替换）补上 `"runner": "codex"`：补上 260，本来就有 0
- [x] 核对数量：总数 260、仍缺 0、值不对 0、无 `.tmp` 残片。备份在 `/tmp/codex-subagent-tasks-backup-*.tar.gz`

---

### Task 2: runner 这条轴 ✅ 已完成 — `1e02eea`

- [x] `CODEX`／`DEEPSEEK`／`RUNNERS`
- [x] `isolation_home(runner, account)`：deepseek → `~/.claude-subagent`；`account is not None` 当场 `ValueError`
- [x] `isolation_roots()`：`find_meta`／`all_metas` 都走它，收齐 deepseek 的家
- [x] `new_meta(task, runner, account, workdir, effort, skills)` + 构造器内校验；`REQUIRED_META_KEYS` 跟着派生
- [x] `ensure_isolation(runner, account)` 按 runner 分叉；deepseek 只建 `tasks`/`reports`/`logs`
- [x] `auth_source(None)` 当场 `ValueError`
- [x] `TestRunnerAxis` 12 条；全量 330 绿

---

### Task 3: 测试护栏 + deepseek 的纯函数 + PID 反查 runner 化 ✅ 已完成 — `3522b20`

**Files:** `codex_sub_agent.py`、`test_codex_sub_agent.py`

- [x] **Step 0: `setUpModule` 的护栏扩到 `claude-deepseek`** — `08a2559`

  审查突变 M21 实测：少一条拒绝时**真的把 `claude-deepseek` 叫起来了**。
  清单提成 `_REAL_AGENT_BINS`，只有一个家。
  **这一条必须排在任何 deepseek 代码之前**，它是后面所有步骤的前提。

- [x] **Step 1: 写失败的测试 —— 纯函数**

  `TestDeepseekRunner`：
  - uuid 是独立 argv 元素（`--session-id` / `--resume` 两处），不是 `=` 连接
  - argv 带 `--dangerously-skip-permissions`、`--output-format stream-json`、`--verbose`
  - argv 里**没有**工作目录（走 `Popen(cwd=)`）；brief 恒在末尾
  - `deepseek_env(home)`：`CLAUDE_CONFIG_DIR` 指向 home；**交出去的 env 里一个模型变量都没有**
  - `finish_deepseek_round`：success 才写报告；打断不写；没有 `result` 事件不写；
    取**最后一条** `result`；尾部坏行不许让解析崩；`result` 为空写空报告（让 judge 判 failed）
  - `deepseek_log_lines`：非 success 补 `ERROR: <subtype>: …`；`permission_denials` 非空补一行；
    一切正常返回 `[]`；补出来的行 `runtime_error_lines` 原样认得
  - `judge` 三态：success→success、denials→suspect（退 3）、打断→interrupted（退 130）
  - `agent_comm(runner)`：codex→`b"codex"`、deepseek→`b"claude"`、**未知 runner 抛 `ValueError`**
  - `split_complete_lines(buffer)`：返回 `(完整行, 残片)`；**残片要攒着**，
    不许丢、也不许当成完整行（突变「`join`+`splitlines`」必须红）
  - `new_session_id()`：两次不同、长度 36

- [x] **Step 2: 写失败的测试 —— PID 反查的 6 个调用点**

  `TestFindAgentPid`（真陪练进程，照抄既有 `TestPid` 的形状）：
  - `find_agent_pid(DEEPSEEK, uuid)` 命中 `comm=claude` 且 argv 里有该 uuid 的进程
  - **不相干的 claude 进程不许命中**（spec 判据 9 后半）：argv 里没有这个 uuid 的 `claude` 进程找不到
  - `find_task_agent_pid(runner, home, task)` 的 needle 按 runner 取：
    codex → 报告路径（**元数据不在也查得到**，这条不能丢）；deepseek → 元数据里的 session id
  - **`_wait_previous_round_ends` 用的是磁盘上那一轮的 uuid，不是传进来的 meta**（承重）：
    构造「磁盘上记着旧 uuid、传进来的 meta 是新 uuid、旧 uuid 的进程还活着」，断言它还在等
  - `cmd_status` / `cmd_stop` 对一个 deepseek 任务真的有效（spec 判据 11）

- [x] **Step 3: 跑测试确认失败**

- [x] **Step 4: 实现**

```python
DEEPSEEK_BIN = "claude-deepseek"
DEEPSEEK_EFFORT = "max"          # 用户的硬约束，写成常量不散落字面量

# claude-deepseek 开头会拒绝「别处在定模型」：它扫环境里所有
# ^(ANTHROPIC_|CLAUDE_)[A-Z0-9_]*MODEL[A-Z0-9_]*$ 的变量，值不等于它自己那个
# 就退 2。**用户的场景正是「我现在正在用原生的 Claude Code」**——那一刻
# ANTHROPIC_MODEL 就是 claude-*，原样透传 os.environ 会让整个功能一次都跑不起来。
# 实测：ANTHROPIC_MODEL=claude-opus-5 claude-deepseek -p "…" → 退 2。
# 删掉它们不改变正确性：wrapper 自己 export 全套模型变量。
_MODEL_ENV = re.compile(r"^(?:ANTHROPIC_|CLAUDE_)[A-Z0-9_]*MODEL[A-Z0-9_]*$")


def deepseek_env(home):
    """deepseek 侧的隔离：`CLAUDE_CONFIG_DIR` + 清掉继承来的模型变量。"""
    env = {k: v for k, v in os.environ.items() if not _MODEL_ENV.match(k)}
    env["CLAUDE_CONFIG_DIR"] = str(home)
    return env
```

  其余：`_deepseek_base_argv(effort)`、`build_deepseek_argv(effort, session_id, brief)`、
  `build_deepseek_resume_argv(effort, session_id, brief)`、`_last_result_event`、
  `deepseek_log_lines`、`finish_deepseek_round(round_text, report_path, log_path)`、
  `agent_comm`、`split_complete_lines`、`new_session_id`。

  `find_codex_pid(report_path)` 拆成两个：

```python
def find_agent_pid(runner, needle):
    """comm 按 runner 取、needle 按 argv 元素**精确比对**（绝不用正则）。"""

def find_task_agent_pid(runner, home, task):
    """这个任务此刻有没有活着的 agent。**needle 一律从磁盘派生，不收调用方的 meta。**

    codex     报告路径由 (home, task) 派生——**元数据不在也查得到**，这条不能丢：
              元数据被手删、上一轮 codex 还占着会话时，早返回会当场放行
    deepseek  session id 只住在元数据里；run 的每一轮都是新 uuid，
              传进来的那个查不到上一轮 → 等待变成空操作 → 两个写者共写一份日志
    """
```

  6 个调用点全部改成 `find_task_agent_pid`：`_wait_previous_round_ends`（轮询 + 超时诊断）、
  `cmd_run` 的存活闸、`cmd_status`、`cmd_resume`、`cmd_interrupt_and_resume`、`cmd_stop`。
  `_wait_previous_round_ends` 签名加 runner（由 `run_codex` 从 `meta["runner"]` 传）。

- [x] **Step 5: 跑全量 + 提交**

---

### Task 4: 接线 —— `run_codex` 里真的分叉（最严重的一条）✅ 已完成 — `94a778e`

**这一步是 v2 整个漏掉的东西。** 没有它，Task 3 那些函数一个调用点都没有。

- [x] **Step 1: 写失败的集成测试**（spec 判据 13 的五条，假 `Popen` 驱动**真实** `run_codex`）

```python
class TestDeepseekWiring(_HomeSandbox):
    """假 Popen 驱动真实 run_codex。**断言的是产物，不是「那个函数被调用了」。**"""
    # ① 交给子进程的 env 里有 CLAUDE_CONFIG_DIR 指向隔离目录，且**没有任何模型变量**
    #    （不接上 deepseek_env，子代理就看得见整台机器的 skill/MCP/hook）
    # ② Popen 收到 cwd=meta["dir"]（claude -p 没有 --cd，产物必须落在 --dir 里）
    # ③ 日志开头有 DEEPSEEK_LOG_NOTE
    # ④ thinking_tokens 行不在日志里；其余事件一字不改；**尾部残片留在日志里**
    # ⑤ success → 报告出现；error_during_execution → 报告**不**出现
    # ⑥ permission_denials 非空 → 日志里有 ERROR: 行，且它落在**本轮边界之内**
    #    （judge 看得见），run_codex 回传的 Round 文本里就有
    # ⑦ **codex 那条路径一个字节没改**：同样喂一行 thinking_tokens，
    #    codex 的日志里它必须原样在（反向护栏，比 assertIsNone(log_line_filter) 强）
```

- [x] **Step 2: 跑测试确认失败**

- [x] **Step 3: 实现接线**

  `run_codex` 里三处按 runner 分叉 + 一处两侧都加：

| 位置 | 改什么 |
|---|---|
| `env = codex_env(home)` | → `codex_env(home) if runner == CODEX else deepseek_env(home)` |
| `Popen(...)` | 加 `cwd=meta["dir"]`。**两侧都传，少一个分支**：codex 本来就有 `--cd`，多传无害；deepseek 非它不可 |
| 分隔符之后 | deepseek 多写一行 `DEEPSEEK_LOG_NOTE`（在 `start_offset` **之前**取，所以它不进本轮文本；`read_last_round` 那条路会看到它，无害——它匹配不上任何 `_ERR_*`） |
| tee 之后 | deepseek 调 `finish_deepseek_round(read_round(...), report, log_path)`，**在回传 `Round` 之前**——补的 `ERROR:` 行必须落在本轮边界之内，否则 judge 看不见 |

  `_tee_until_exit` 顶部加一句 deepseek 早返回，**codex 那一支一个字节不动**：

```python
def _tee_until_exit(proc, log, home, task, meta):
    if meta["runner"] == DEEPSEEK:
        _tee_lines(proc, log, keep_deepseek_line)
        return
    # ↓↓↓ 以下 codex 那一支一个字节未改 ↓↓↓
```

  `_tee_lines(proc, log, keep)` 攒 buffer 走 `split_complete_lines`；
  **残片在 EOF 后原样写出**——它是「这一轮被杀在半路」的现场证据。

- [x] **Step 4: 拿真实现场日志验过滤比例**

```bash
python3 -c "
import codex_sub_agent as ca, pathlib, glob
f = sorted(glob.glob('/home/xy/.claude/jobs/*/tmp/ds-review/out.jsonl'))
if not f: print('没有现场日志，跳过'); raise SystemExit
raw = pathlib.Path(f[-1]).read_text(errors='replace').splitlines()
kept = [l for l in raw if ca.keep_deepseek_line(l.encode())]
print(f'行 {len(raw)}→{len(kept)}（压掉 {100-100*len(kept)/len(raw):.1f}%）；'
      f'字节 {sum(map(len,raw))}→{sum(map(len,kept))}（压掉 {100-100*sum(map(len,kept))/sum(map(len,raw)):.1f}%）')
"
```
  **两个口径不一样，别混着说**：审查实测行数压掉 99.8%、字节压掉 92.4%
  （`thinking_tokens` 行短而密）。预期行数 ≥99%、字节 ≥90%。

- [x] **Step 5: 跑全量 + 提交**

---

### Task 5: CLI —— `--runner`、三条硬拒绝、uuid 的生命周期 ✅ 已完成 — `400d245`

- [x] **Step 1: 写失败的测试**

```python
class TestRunnerCLI(_HomeSandbox):
    # setUp 里要先把 ~/.codex-accounts/acct2/auth.json 建出来：
    # --account 的 choices 是扫盘来的，不建目录连 argparse 都过不去。

    def test_runner必填(self): ...                    # 不给 --runner → SystemExit
    def test_deepseek给account当场拒(self): ...        # 含「账号」
    def test_deepseek的effort不是max就拒(self): ...    # 含「max」
    def test_codex仍然必须给account(self): ...

    def test_被拒的调用不留任何盘上副作用(self):
        # v2 那条 `test_三条拒绝排在任何状态变更之前` 是**空测试**：审查突变实测，
        # 把三条拒绝整个删掉、或挪到 ensure_isolation 之后，它照样绿——它构造的
        # 场景根本到不了迁移分支（那个分支的门是 --account auto，而三条拒绝与
        # auto 互斥），于是先撞上跨账号护栏、照样抛 Rejected、元数据照样在。
        # 换成钉「盘上什么都没多出来」：ensure_isolation 是第一个落盘的动作。
        with self.assertRaises(ca.Rejected) as got:
            ca.cmd_run(self._args(task="t", runner="deepseek", account=None, effort="high"))
        self.assertIn("max", got.exception.message, "钉住是哪一条拒绝在说话")
        self.assertFalse((self.home / ".claude-subagent").exists(),
                         "被拒的调用不许建隔离目录")

    def test_resume和stop都不收runner(self): ...       # 从元数据查出来

    def test_每次run都铸新uuid_落元数据_argv用的就是它(self):
        # 实测复用同一个 session-id 会得到 `Error: Session ID … is already in use.` 退 1。
        # 同一任务名 run 两次 → 两个 uuid 不同；且 argv 里 --session-id 后面跟的
        # 就是元数据里那个（两处不许各算各的）。

    def test_同名任务不许跨runner复用(self):
        # find_meta 会数出两份并拒绝；而跨 runner 没有迁移路径
        # （--account auto 只在 codex 的账号之间搬）。当场拒，别让这个状态建起来。
```

- [x] **Step 2: 跑测试确认失败**

- [x] **Step 3: 实现**

  - `build_parser`：`--runner` 必填 `choices=list(RUNNERS)`；`--account` 改可选
    （argparse 表达不了「A 必填当且仅当 B 是某值」）
  - `cmd_run` **最前面**三条硬拒绝，排在 `find_meta` / `ensure_isolation` / `new_meta` 之前
  - **uuid 在 `new_meta` 里铸**（不是在 `cmd_run` 里）：
    `"session_id": new_session_id() if runner == DEEPSEEK else None`。
    理由和 writer 身份自取同一条——本函数产出的是「一份**此刻开始**的任务的完整记录」，
    而「这一轮是哪个会话」就是这份记录的一部分。放在 `cmd_run` 里等于给它开第二个家，
    而且「复用任务名 = 新一轮 = 新 uuid」就退回成一条要记住的软约定
  - `cmd_run` 的跨 runner 护栏 + 跨账号护栏（后者的 `isolation_home` 要跟着 runner 走）
  - **撞额度上限那一支限 codex**：`accounts_by_availability()` / `write_usage_limit`
    都是账号维度的事，deepseek 走进去会往 `~/.claude-subagent` 写一条没有意义的限流记录
  - `_resume_round` / `cmd_resume` / `cmd_interrupt_and_resume` 按 `meta["runner"]` 选 builder
  - `check_can_resume` 第一道闸的注释拆开：那段「session_id 要等 codex 第一块输出
    才落盘、实测 4.06 秒」是 **codex 专属**；deepseek 的 uuid 从 `new_meta` 那一刻就在
  - `status_row` 第二列复用成 `runner[:account]`：codex → `codex:acct2`，deepseek → `deepseek`。
    **不加第五列**——`split(maxsplit=4)` 是机器切分的契约，加一列会把它改掉。
    顺带修掉一个真 bug：deepseek 的 `account` 是 `None`，`f"{None:<8}"` 当场 `TypeError`

- [x] **Step 4: 跑全量**

- [x] **Step 5: 真跑一次冒烟 —— 必须产出真文件 + 必须核对隔离**

```bash
WD=$(mktemp -d)
printf '在当前工作目录创建 proof.txt，内容写 SMOKE-OK。然后回答：完成\n' > /tmp/smoke-ds.md
./codex_sub_agent.py run --task smoke-deepseek --runner deepseek --dir "$WD" \
  --brief /tmp/smoke-ds.md --effort max --no-skill
echo "退出码 $?"
cat "$WD/proof.txt"                      # ← **必须是 SMOKE-OK**
./codex_sub_agent.py status smoke-deepseek
ls ~/.claude-subagent/                   # ← 隔离目录只有 tasks/reports/logs
grep -c thinking_tokens ~/.claude-subagent/logs/smoke-deepseek.log   # ← 必须是 0
```
  **只断言「报告非空」挡不住「零工作量的成功」**——必须核对文件真的落在 `--dir` 里。
  **只跑一次，别反复跑。codex 侧不跑冒烟**：三个账号全部限流。

- [x] **Step 6: 提交**

---

### Task 6: 改名 —— 三类，不是两类 ✅ 已完成 — `f4b085e`（安全网）+ `871d1f4`（改名）

- [x] **Step 1: 先把盘上契约钉住**（这条测试改名前就该绿，它是安全网）

```python
def test_盘上契约的字面量一个都没改(self):
    self.assertEqual(ca.ROUND_MARK, "===== codex-sub-agent ")
    self.assertEqual(ca.INTERRUPT_MARK,
                     "----- codex-sub-agent 本轮被 INT 打断，上下文保留，可 resume -----")
    self.assertEqual(ca.isolation_home(ca.CODEX, "default").name, ".codex-subagent")
```

- [x] **Step 2: 逐处分类**（spec 第二节那张表）

```bash
grep -n "codex-sub-agent\|codex_sub_agent" codex_sub_agent.py test_codex_sub_agent.py \
  SKILL.md README.md ~/.claude/settings.json 2>/dev/null
```

| 类别 | 例子 | 改不改 |
|---|---|---|
| 工具的名字 | 命令名、`prog=`、帮助文本、模块 docstring、SKILL.md／README、**9 处 `[codex-sub-agent]` 打印前缀**，以及**3 处把命令名写进给人看的下一步建议**（`codex_sub_agent.py:1713/1956/2166`，例如 `` 先 `codex-sub-agent stop {task}` ``）——不改就是打印一条不存在的命令 | **改** |
| 盘上契约 | `ROUND_MARK`、`INTERRUPT_MARK`（含 `:197/:200` 两行**复述它们字面量的注释**）、`~/.codex-subagent*` | **不改** |
| 只写不读的文本 | `CONFIG_NOTE`（写进隔离目录的 config.toml，无人读回） | 可改 |

  **`test_codex_sub_agent.py:2414` 那条 `f"codex-sub-agent {cmd}"` 要改。**
  v2 把它判成「盘上契约，不改」是**错的**——它是 `TestSkillDocDoesNotRepeatCode`
  在钉 SKILL.md 里的**命令名**。SKILL.md 改了它必红，照 v2 的字面执行会卡死。

- [x] **Step 3: 改模块名（下划线那个可以全局替换）**

```bash
git mv codex_sub_agent.py sub_agent_runner.py
git mv test_codex_sub_agent.py test_sub_agent_runner.py
sed -i 's/codex_sub_agent/sub_agent_runner/g' sub_agent_runner.py test_sub_agent_runner.py
```
  `codex_sub_agent`（下划线）是模块名，**不出现在任何盘上契约里**。
  `codex-sub-agent`（连字符）**不可以**全局替换——它在两个 MARK 里。

- [x] **Step 4: 逐处改连字符那个**，然后
      `python3 -m unittest test_sub_agent_runner -k 盘上契约 -v`（必须仍然绿）+ 全量

- [x] **Step 5: 文档**：SKILL.md 的账号那一格拆成 runner + 账号；
      写明 deepseek 的 effort 只能是 max（**说事实，不写「记得传 max」**——代码已经硬拒绝了）。
      README 同步，含安装段那条命令。

- [x] **Step 6: 提交**

- [x] **Step 7: 留给协调者在主 checkout 上做的两件事（本计划范围之外，写在这里免得丢）**

      1. `mv ~/.claude/skills/codex-sub-agent ~/.claude/skills/sub-agent-runner`
         ——**worktree 里不能做**（会挪走主仓目录、连带 worktree 注册失效）
      2. `ln -sfn .../sub-agent-runner/sub_agent_runner.py ~/.local/bin/sub-agent-runner`
         并删掉旧的 `~/.local/bin/codex-sub-agent`（现指向
         `/home/xy/.claude/skills/codex-sub-agent/codex_sub_agent.py`）。
         **不修就是「旧命令坏了、新命令不存在」**，README 的安装段也是这条命令

---

## 审查那九条「值得商榷」的处置

| # | 审查说 | 处置 | 理由 |
|---|---|---|---|
| 1 | `agent_comm` / `log_line_filter` 对未知 runner 静默返回默认值，与 `_require_enum` 教条相反 | **采纳** | `agent_comm` 改成抛 `ValueError`；`log_line_filter` **整个删掉**——tee 自己按 runner 分叉，这个函数就成了零消费者的实体（奥卡姆） |
| 2 | 两个 builder「签名一致」是空头承诺 | **采纳** | `build_deepseek_argv` 不收 `dir_abs`——它根本不进 argv。收一个不用的参数是骗调用方 |
| 3 | spec 判据 9 没有对应测试 | **采纳** | Task 3 Step 2 补两条：打断后 `status` 真变 `interrupted`；不相干的 `claude` 进程不许命中 |
| 4 | 判据 11 只测到 `find_meta` 那层 | **采纳** | Task 3 Step 2 补 `cmd_status` / `cmd_stop` 对 deepseek 任务的测试 |
| 5 | `DEEPSEEK_LOG_NOTE` 定义了没人写 | **采纳** | Task 4 Step 3 写它，Step 1 的集成测试第 ③ 条钉它 |
| 6 | Task 4 Step 4 的百分比口径 | **采纳** | 行 / 字节两个口径分开说（实测 99.8% / 92.4%） |
| 7 | `auth_source` 在 Files 里但没代码没测试 | **已做** | Task 2 已实现（`None` 抛 `ValueError`）并有测试 |
| 8 | Task 3 没有 `deepseek_env` 的测试、冒烟也没验隔离 | **采纳** | Task 3 Step 1 加 env 测试（含模型变量清除）；Task 5 Step 5 冒烟核对隔离目录与过滤 |
| 9 | `new_meta` 贴的代码带省略号 | **已做** | Task 2 按真实代码实现 |

**与协调者指示的一处偏离**：uuid 铸在 `new_meta` 而不是 `cmd_run`。
要求的四条（run 铸新 uuid、落元数据、两次 run 不同、argv 用元数据里那个）全部满足，
而放在构造器里让「复用任务名 = 新一轮 = 新 uuid」从软约定变成结构事实——
和 `new_meta` 里 writer 身份自取那条注释是同一条论证。


---

## 执行结果（2026-09-23）

| Task | 状态 | commit |
|---|---|---|
| 1 迁移 260 份元数据 | 完成 | 无（一次操作，不是代码） |
| 2 runner 轴 | 完成 | `1e02eea` |
| 3 纯函数 + PID 反查 | 完成 | `08a2559`（护栏）+ `3522b20` |
| 4 接线 + 集成测试 | 完成 | `94a778e` |
| 5 CLI + 冒烟 | 完成 | `400d245` |
| 6 改名 | 完成 | `f4b085e` + `871d1f4` |

全量测试：**`Ran 400 tests` / `OK`**（基线 318 → 400，净增 82 条）。

**冒烟（唯一一次真跑，$0.12）**：

```
ANTHROPIC_MODEL=claude-opus-5 ./sub_agent_runner.py run --task smoke-deepseek \
    --runner deepseek --dir /tmp/smoke-ds-Xss5 --brief /tmp/smoke-ds.md --effort max --no-skill
→ 退出码 0
→ /tmp/smoke-ds-Xss5/proof.txt 内容正好 8 字节 `SMOKE-OK`
→ status: smoke-deepseek  deepseek  success  0  /tmp/smoke-ds-Xss5  正常收尾，本轮日志无未分类错误
→ 隔离核对：init 事件里 mcp_servers=[]、skills 里一个用户 skill 都没有
→ 模型变量核对：设了 ANTHROPIC_MODEL=claude-opus-5 也没被 claude-deepseek 退 2
```

**留给协调者在主 checkout 上做的三件事**（worktree 里做不了），**顺序不能反**：

1. `mv ~/.claude/skills/codex-sub-agent ~/.claude/skills/sub-agent-runner`
2. `ln -sfn ~/.claude/skills/sub-agent-runner/sub_agent_runner.py ~/.local/bin/sub-agent-runner`
   并删掉旧的 `~/.local/bin/codex-sub-agent`（现指向
   `/home/xy/.claude/skills/codex-sub-agent/codex_sub_agent.py`，合并后就是死链）
3. **把迁移再跑一遍**（下面那段脚本），然后核对 `status` 列得出全部任务

### 第 3 步不是保险，是必须的——Task 1 的「一次操作」有个计划和 spec 都没看见的前提

迁移只有在**没有第二份代码还在写元数据**时才是一次性的。而旧命令
（`~/.local/bin/codex-sub-agent` → 主 checkout 的 `codex_sub_agent.py`）**一直是活的**：
它不认识 `runner` 字段，每写一份新元数据就重新开一个洞。

**这不是假想，本次执行期间真的发生了**：05:38 迁移完 260 份，06:38 协调者用旧命令
起了两个 spec 审查任务（`spec-review-schema`、`spec-review-schema-a2`），
它们的元数据**没有 `runner`**。合并之后的后果是立刻可见的：

```
$ sub-agent-runner status
Rejected: …/tasks/spec-review-schema-a2.json 缺字段 ['runner']，元数据坏了——删掉它重新 run
```

**爆炸半径是整个列表**（`_load_meta` 上方那段注释早写过）：一份坏元数据让不带任务名的
`status` **一个任务都列不出来**，263 个一起陪葬；而工具给的唯一建议是「删掉它重新 run」
——那等于丢掉那个会话。

所以补迁移必须排在第 2 步**之后**（那一刻旧命令才真正死掉，不会再有新的一份）：

```bash
python3 -c "
import sys; sys.path.insert(0, '$HOME/.claude/skills/sub-agent-runner')
import json, pathlib, sub_agent_runner as ca
done = 0
for home in [pathlib.Path.home() / n for n in
             ('.codex-subagent', '.codex-subagent-acct2', '.codex-subagent-acct3', '.claude-subagent')]:
    d = home / 'tasks'
    if not d.is_dir(): continue
    for q in sorted(d.glob('*.json')):
        m = json.loads(q.read_text())
        if 'runner' in m: continue
        m['runner'] = 'codex'; ca.write_meta(home, m['task'], m); done += 1
metas = ca.all_metas()
print('补上', done, '| 总数', len(metas), '| 仍缺',
      sum(1 for _, m in metas if 'runner' not in m))
"
sub-agent-runner status | grep -c '^[a-zA-Z0-9]'   # 应与上面的总数一致
```

**合并前如果还要用这个工具派任务，就用 worktree 里的 `./sub_agent_runner.py`**
（它认 `runner`），别用旧命令——每用一次就多一份要补的元数据。

**顺带产生的一条真实盘上状态**：冒烟留下了任务 `smoke-deepseek`（在
`~/.claude-subagent`），加上协调者 06:38 用旧命令起的两个 spec 审查任务，
`all_metas()` 现在是 **263**（260 + 1 + 2，后三份都已补上 `runner`）。
要清掉就删 `~/.claude-subagent/{tasks,reports,logs}/smoke-deepseek.*`。
