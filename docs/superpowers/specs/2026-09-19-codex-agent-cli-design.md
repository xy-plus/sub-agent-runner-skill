# codex-agent CLI 设计

日期：2026-09-19　状态：已批准，待实现

## 1. 问题的本质

现状：`codex-sub-agent` skill 用 250 行文档描述**一条必须每次手写正确的 `codex exec` 命令**，
外加一整套监控／判据／续跑的仪式。里面有 12 条硬性约束（`--cd` 绝对路径、`</dev/null`、
`-o` 绝对路径、不加 `&`、不加 timeout、只能 `kill -INT`、exit 1 不等于失败、resume 不认
`--sandbox`……），**每一条都靠调用方读文档时记住**。

不方便的根因不是「它不是 MCP」，而是**约束写在文档里（软约定），不在代码里（硬约束）**。
每次调用要花 250 行上下文把这些知识读进来，然后靠模型照着念对。

**解法：把这 12 条约束编译进一个可执行文件。** 调用方只提供任务本身的信息
（干什么、在哪干、多难），工具负责其余全部。

## 2. 为什么不是 MCP（非目标）

这套东西的价值有三条：① codex 干活 ② claude 不等、去干别的 ③ 干完自动通知。

| MCP 形态 | 代价 |
|---|---|
| 同步等待 codex 结束 | 任务动辄几十分钟，要么超时要么把 claude 阻塞在原地 → ② 没了 |
| 立即返回 + 轮询 | 完成通知来自 harness 对 `Bash(run_in_background)` 的追踪，MCP 没有这个通道 → ③ 没了 |

MCP 能买到的只有「参数有 schema」和「不用读长 skill」，CLI 同样给得到，且不丢后台通道。
**结论：启动路径必须留在 `Bash(run_in_background)` 上。**

（附：codex 0.154.0 的 `codex mcp` 只是管理**外部** MCP 服务器，不提供「把自己暴露成 MCP server」。）

## 3. 形态

一个 CLI + 一个瘦 skill：

| 组件 | 职责 |
|---|---|
| `codex-agent`（可执行） | 承载全部硬约束：命令组装、隔离目录、存活判定、成败判据、续跑、停止 |
| `SKILL.md`（~40 行） | 只讲**人／模型才能决定的事**：派什么任务、effort 怎么分档、四条命令长什么样 |

语言选 **Python3**：`argparse` 的 `required=True` + `choices=` 天然实现「强制显式」，
错误消息免费；判据逻辑是纯函数，可零成本单测（不花 codex token）。

## 4. 目录与命名

一个词贯穿 skill 名、目录名、命令名：`codex-agent`。

```
~/.claude/skills/codex-agent/          # 由 codex-sub-agent 改名而来
├── SKILL.md
├── codex_agent.py                     # 实现（下划线：可被测试 import）
└── test_codex_agent.py                # stdlib unittest，零依赖
~/.local/bin/codex-agent -> ~/.claude/skills/codex-agent/codex_agent.py
```

每个账号一个隔离目录（沿用现状，不新造）：

```
$D = ~/.codex-subagent            (account=default)
     ~/.codex-subagent-<账号>      (account=acct2 / acct3 / …)
├── config.toml        # 禁软链；内容不归我们管（codex 自己往里写 trust_level）
├── auth.json -> 该账号登录态
├── skills/  plugins/  # 存在即可，保持不含用户 skill/plugin
├── tasks/<任务>.json   # 任务元数据（见 §7）
├── reports/<任务>.md   # codex 收尾自述（`-o` 落点，是 agent 的最后一条消息，通常是散文不是 JSON）
└── logs/<任务>.log     # 全量输出，**追加**，每次调用前写一行分隔符
```

`reports/`、`logs/`、`tasks/` 全在**仓库外**——过程文件进不了 git，`git add -A` 收不到。

## 5. CLI 契约

```
codex-agent run    --task N --dir D --brief F --effort E --account A
codex-agent status [N]
codex-agent resume N --brief F --effort E
codex-agent stop   N
```

### 参数规则

| 参数 | 规则 |
|---|---|
| `--task` | 字符集限死 `[A-Za-z0-9._-]+`，**由 argparse 的 `type=` 把关**，四个子命令都绕不过。重名规则见下 |
| `--dir` | 自动 `realpath` 成绝对路径；不是目录即拒绝 |
| `--brief` | **必须是文件路径**，不收内联字符串（杜绝引号地狱） |
| `--effort` | `choices={low,medium,high,xhigh,max}`，无默认值。**这是对 codex 全集（`minimal/low/medium/high/xhigh/max/ultra`）的刻意裁剪**；`choices` 是唯一守门员——实测 codex 对 `-c model_reasoning_effort=bogus` 静默接受、banner 照打 |
| `--account` | `choices` 在运行时由 `~/.codex-accounts/` 扫描得出 + `default`，无默认值 |

`resume` / `stop` / `status` **不收 `--account`**：账号从任务元数据**查出来**，不是默认值。
`status` 省略任务名 = 列出所有隔离目录下的全部任务。

### 任务名为什么要限字符集

任务名同时是**文件名**和 **`pgrep -f` 的匹配模式**，两边都会被奇怪字符咬（2026-09-19 实测）：

| 传进去的名字 | 后果 |
|---|---|
| `../escape` | 元数据写到 `tasks/` **外面**去了 |
| `a/b` | 裸 `FileNotFoundError`，不是人话 |
| `a\|b` | `pgrep -f` 把它当**正则或**，命中任意含 `b.json` 的进程 → **`stop` 把 SIGINT 发到别人的 codex 上** |
| `fix(api)` | pgrep 正则分组，匹配语义静默改变 |

第三行正是 §9 发誓要避免的后果（「永不 `pkill -f`，共享机器会误杀」），从任务名这个后门
又放了进来。所以收窄放在 `argparse` 的 `type=` 上——结构上不可绕过，不是校验函数靠调用方记得调。

### 重名规则

| 情况 | 处置 |
|---|---|
| 同名任务**还在跑** | 拒绝 |
| 同名任务在**同一账号**下、已结束 | 允许复用：报告被覆盖，日志是追加的所以上一轮仍在 |
| 同名任务在**别的账号**下 | 拒绝，并告诉你它属于哪个账号 |

第三行是硬约束不是洁癖：`find_meta` 按账号顺序查，跨账号同名会留下一份**再也够不着的孤儿
元数据**，而 `run` 打印的「上一轮会被覆盖」在跨账号时是**假话**（两个 home 的报告路径不同）。

### 退出码即结论

| 码 | 含义 |
|---|---|
| `0` | success |
| `1` | failed |
| `2` | **护栏拒绝或参数写错**（与 argparse 同码，两者是同一类事） |
| `3` | suspect |
| `4` | running（`status` 用；任务还没结束） |

`2` 单独留给「调用方写错了」，**绝不与 `failed` 共用**——否则脚本分不清
「`--dir` 写错了」和「codex 真的跑失败了」。`status` 列多个任务时按**严重度**取最坏
（success < running < suspect < failed），不按数值取 max。

### 不提供的参数（刻意的）

`--timeout`、`--background`、`-o`、`--log`、`--model`、`--sandbox` —— 这些要么会造成误用，
要么由工具派生。模型固定 `gpt-6-astra`，难度只由 `--effort` 分档。

### run 的行为

1. 校验参数（任务名字符集由 argparse 把关）
2. 校验／创建隔离目录（§6）
3. 查重任务名（§5「重名规则」）
4. 读 brief，**自动前置**「不得使用任何 skill，除非本 brief 明确指定。」并打印一行提示
   （不做隐形魔法）
5. **删掉旧报告** —— 让「报告存在」变成关于本次调用的事实（§8）
6. 日志追加一行分隔符，写元数据
7. 起 codex：`stdin=DEVNULL`、`start_new_session=True`、装信号 handler（§9），
   输出 tee 到屏幕和日志
8. 从输出里提取 session id 写进元数据（**边跑边写**，不等结束——包装进程被杀时
   没写进去的话，这一轮就再也 resume 不回来）
9. **进程结束后自己跑一遍判据（§8），打印结论，退出码＝判据结论**

`resume` 除了第 3 步换成「必须已结束且工作目录还在」，其余相同。

第 8 步是核心收益：`exit 1 ≠ 失败`这条最反直觉的知识被彻底消化，
harness 的完成通知里直接带成败结论，happy path 下无需再敲 `status`。

### 固定的 codex 参数

```
run:    codex exec --cd <abs> -m gpt-6-astra -c model_reasoning_effort=<E>
        --sandbox danger-full-access -c approval_policy=never -c project_doc_max_bytes=0
        --skip-git-repo-check --disable plugins --color never -o <D>/reports/<N>.md <brief>
resume: codex exec --cd <abs> resume <session_id> …同上，但
        -c sandbox_mode=danger-full-access   # resume 不认 --sandbox（退出码 2）
```
环境：`CODEX_HOME=<D>`、`CODEX_SQLITE_HOME=~/.codex`（会话索引共享，resume 才找得到）。
`--cd` 必须在 `resume` **之前**（放后面 clap 直接拒收）。

## 6. 隔离目录不变量（每次 run/resume 都校验）

| 不变量 | 违反时 |
|---|---|
| `config.toml` 是普通文件，**不是软链** | 拒跑：软链主配置会把 MCP/plugins/hooks 全带回来，隔离当场失效 |
| `auth.json` 软链指向该账号登录态 | 缺失则创建；账号无登录态则拒跑 |
| `skills/`、`plugins/`、`tasks/`、`reports/`、`logs/` 存在 | 缺失则创建 |
| `~/.agents/skills/` 为空 | 拒跑：那是 `CODEX_HOME` **管不到**的共享扫描根，放了东西 codex 就看得见，隔离的前提不成立 |

**`config.toml` 的内容不是不变量，不许校验也不许重写。** 2026-09-19 实测：codex 自己往这个
文件里写 `[projects."…"] trust_level = "trusted"`，`~/.codex-subagent` 已经累积了 19 段。
校验内容则第二次 run 就失败，重写则抹掉 codex 自己的 trust 状态。缺失时只创建一个**仅含
说明注释的空配置**，让打开它的人知道这文件为什么必须是本地的。

**`model` / `model_reasoning_effort` / `sandbox_mode` / `approval_policy` 一律不写进
`config.toml`**——CLI 每次都显式传，config 里再存一份就是同一条事实有两个家，而且是个会被
静默覆盖的缺省值。

## 7. 任务元数据 `tasks/<任务>.json`

```json
{"task": "...", "account": "...", "dir": "/abs/...", "effort": "low",
 "session_id": "01a0…", "started_at": "..."}
```

**刻意不存 `pid`。** 存活判定必须每次从 `pgrep` + `comm` 重新反查（见 §8），
存一个会过期、还会被系统复用的 PID，只会诱导别人犯这个设计本来要防的错。
`resume` 会把 `effort` 写回，元数据始终描述最后一次调用。

存在的理由：`resume` 必须回到**同一个目录、同一个会话**。有元数据就不用重新给 `--dir`，
也就不可能 resume 到错的目录去。它同时是 `status` 无参时的任务清单来源。

## 8. 判据（`run` 收尾与 `status` 共用一个纯函数）

| 状态 | 条件 |
|---|---|
| `running` | 真实 PID 存活 |
| `success` | 报告存在且非空 + 本轮日志无未分类错误 |
| `suspect` | 报告存在且非空 + 有未分类错误 → 打印那几行，交给人判断 |
| `failed` | 报告缺失或为空（＝没正常收尾），`reason` 说明为何 |

### 「报告存在」必须是关于**这一次调用**的事实

2026-09-19 实测：codex **只在正常收尾时**写 `-o` 指定的文件，启动时**不 truncate**。
所以 run 成功写下报告、随后 resume 秒死于写锁时，判据会读到**上一轮的旧报告**并判 `success`
——工具在说谎。真实日志里有 5 份样本正是这条路径。

**所以 `run` 和 `resume` 都在 spawn codex 之前先删掉报告文件。** 本工具是它唯一的创建者，
于是「报告存在」重新变成一句关于本次调用的真话。代价是失败的 resume 会连带毁掉上一轮的报告
——可以接受：日志是追加的，上一轮的内容仍在日志里。

### 真实 PID 怎么拿（不能靠 `$!`、不能靠日志）

`pgrep -u <当前用户> -f "reports/<任务>.md"` → 再按 `ps -o comm=` 严格等于 `codex` 收窄
→ `kill -0` 确认。报告路径在 codex 的 argv 里且按任务唯一，这是「按任务命名」的第二个用处。

- `$!` 拿到的是包装链最外层，不是 codex（2026-09-17 实测：`$!`=254151，codex=254153）。
- **`comm` 过滤是承重的**：2026-09-19 实测，`pgrep -f <报告路径>` 确实命中发命令的 bash 自己。
- **`-u` 过滤也是承重的**：本机有其他用户同时在跑 codex。
- `kill -0` 的 `PermissionError`（EPERM）意思是**进程存在**，只有 `ProcessLookupError`（ESRCH）
  才是已退出。把 EPERM 当死，就会误判「已结束」而去 resume 一个还在跑的会话。

### 怎么认 codex 自己的错误（2026-09-19 对 106 份真实日志全量统计）

日志里**混着 brief 原文和 codex 转述的子进程输出**，裸 `grep -E 'ERROR|WARN'` 大面积误报：
cargo 的 `error[E0599]`、pytest 的 `E   KeyError`、brief 里引用 TDD skill 的
`**Test errors?**`、markdown 标题 `## Warning Signs`。

codex 自己的错误有**三种锚定形式**，少认一种就等于判据失效：

| 形式 | 剥 ANSI 后的样子 | 语料行数 |
|---|---|---|
| A 用户层 | `ERROR: Reconnecting... 2/5` | 23 |
| B tracing | `2026-09-17T14:35:32.578919Z ERROR codex_core::session: Failed to create session: thread-store conflict: …` | 92 |
| C 顶层致命 | `Error: thread/resume: … already has an active writer (code -32600)` | 5 |

形式 C 的首字母是大写 `Error:`，大小写敏感的 `^ERROR:` 匹配不到它——而它正是 §9 整节在讲的
那个错误。

**形式 B 按 module target 分类，不按自由文本匹配**（文本会变，target 不会）：

| target | 结论 |
|---|---|
| `codex_models_manager::*` | 良性：模型列表刷新超时，不影响本次运行 |
| `codex_api::endpoint::responses_websocket` | 良性：连接抖动，自己会重连 |
| `rmcp::transport::worker` | 良性 |
| `codex_core::tools::router` | 良性：apply_patch 被拒后重打成功 |
| 其余（含 `codex_core::session*`） | 未分类 → 计入判据 |

形式 A 的良性只有一条：`Reconnecting...` **前缀**（有 `waiting for network` 和 `1/5`~`5/5`
等多种后缀，写整行字面量会漏）。形式 C **一律致命**。

特判进 `reason`（不新增状态）：`You've hit your usage limit`（换账号或等额度）、
`thread-store conflict`（会话被锁，只能新起）。

### 扫多长的日志：本轮的全部，不是末 N 行

日志是**追加**的，每次调用前写一行分隔符：

```
===== codex-agent <run|resume> <任务名> <ISO 时间> =====
```

判据只扫**最后一个分隔符之后**的内容。

旧写法是「扫末 50 行」，那个窗口其实在偷偷承担语义——「运行已经恢复过去的错误不算」。
但这件事现在由 module 分类正经做了（`codex_core::tools::router` 那类本来就是良性），
窗口就成了它的劣化替代品，**去掉**。留着只会让下一个撞上 60 行尾部堆栈的人把 50 改成 500，
然后每一次「已恢复的错误」都静默变成 `suspect`。

### 日志从源头就该是纯文本

固定参数里带 `--color never`。实测 `--color auto`（默认）在输出被重定向时**并不关颜色**，
106 份日志无一例外含 ANSI 转义，于是提 session id 和跑判据要各自剥一遍。
从源头关掉，两个消费方都不再依赖剥离器（解析侧仍保留一个 `strip_ansi` 作防御，
但它不再是承重结构）。

### 不进判据的东西

原 skill 的 `jq -e '.commit // .branch'` **属于任务层，不属于工具层**——那是 brief 要求
codex 填的字段。而且实测 156 份真实报告只有 4 份能解析成 JSON：`-o` 写的是 agent 的最后
一条**消息**，通常是 markdown 散文。所以报告用 `.md` 后缀，`success` 时打印它的**前 5 行**
（对任何格式都成立），要结构化输出是 `--output-schema` 的事，另行决定。

## 9. 续跑与停止

| 命令 | 硬约束 |
|---|---|
| `resume N` | 三道闸：① 状态是 `running` 就**拒绝**（对还在跑的会话 resume，报错和 SIGTERM 锁死一模一样，处置却相反：一个该等，一个该弃）② 元数据里没 session id 就拒绝 ③ **元数据里的工作目录不在了就拒绝**（worktree 被删后 codex 会以 `os error 2` 当场崩——和 `--cd` 传相对路径同款症状，而 `run` 那条路是被人话拒绝的，同一个约束不能只编译一半） |
| `stop N` | **只发 `SIGINT`**，只发给本任务的真实 PID。永不裸 `kill`（SIGTERM 会让 thread 永久锁死、再也 resume 不了），永不 `pkill -f`（共享机器会误杀，且模式会匹配到发命令的 shell 自己） |

### 谁停了包装器都一样：codex 只会收到 INT

`stop` 只发 SIGINT，挡住的只是「用户主动停」。还有一条路没挡：**harness 停掉那个后台 Bash
任务**（TaskStop、会话结束）时包装器吃到 SIGTERM，而 codex 默认与包装器同进程组，
会被同一发组信号直接打到 —— 会话永久锁死。

2026-09-19 实测（包装器**不**转发信号，向整个进程组发 TERM）：

| codex 的进程组 | 结果 |
|---|---|
| 与包装器同组（`Popen` 默认） | codex **收到 SIGTERM** 并退出 → 永久锁死 |
| 独立会话（`start_new_session=True`） | codex **什么都没收到**，继续跑 |

所以两件事必须一起做，缺一个洞就还在：

1. `subprocess.Popen(..., start_new_session=True)` —— 把 codex 挡在组信号之外。
2. 包装器给 SIGTERM 和 SIGINT **都**装 handler，统一转发 **SIGINT** 给 codex，再等它退出。

合起来的保证：**无论谁用什么信号停包装器，codex 收到的永远是 INT，上下文永远可 resume。**

## 10. 测试

| 层 | 内容 | 成本 |
|---|---|---|
| 单测（stdlib `unittest`） | 参数校验、隔离目录不变量（含 config.toml 软链拒绝）、判据函数（用真实 log 片段做 fixture：cargo error／pytest E／markdown 标题 **不得**判成 ERROR；`ERROR: Reconnecting` 判良性；`ERROR: You've hit your usage limit` 判 reason）、argv 组装、session id 提取（带 ANSI） | 零 token |
| 端到端冒烟 | 一次 `--effort low` 的真任务，验证起→收尾→判据→resume 全链路 | 一次低档调用 |

## 11. 迁移

1. 实现 `codex_agent.py` + 单测，全绿。
2. 改写 `SKILL.md` 到 ~40 行：派什么任务、effort 分档表、四条命令。
   250 行里的实测教训（日期／当时怎么炸的）**搬进代码注释，贴在防住它的那行旁边**。
3. 合回 master，目录 `codex-sub-agent` → `codex-agent` 改名，**改名之后**才建
   `~/.local/bin/codex-agent` → `~/.claude/skills/codex-agent/codex_agent.py` 软链。
   顺序不能倒：软链指向工作树的话，工作树一删命令就断。
   脚本需 `#!/usr/bin/env python3` 且有可执行位，这两样都显式做，不靠默认。
   端到端冒烟在改名前用 `python3 <绝对路径>/codex_agent.py` 调用，验的是同一份代码。
4. 端到端冒烟通过后，删除本 spec 与计划文档。
