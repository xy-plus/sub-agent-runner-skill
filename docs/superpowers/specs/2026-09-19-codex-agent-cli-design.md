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
├── config.toml        # 自有文件，禁软链（软链主配置会把 MCP/plugins/hooks 带回来）
├── auth.json -> 该账号登录态
├── skills/  plugins/  # 存在即可，保持不含用户 skill/plugin
├── tasks/<任务>.json   # 任务元数据（见 §7）
├── reports/<任务>.json # codex 收尾自述（`-o` 落点）
└── logs/<任务>.log     # 全量输出
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
| `--task` | 全局唯一。`run` 前扫描**所有**隔离目录，重名即拒绝 |
| `--dir` | 自动 `realpath` 成绝对路径；不是目录即拒绝 |
| `--brief` | **必须是文件路径**，不收内联字符串（杜绝引号地狱） |
| `--effort` | `choices={low,medium,high,xhigh,max}`，无默认值 |
| `--account` | `choices` 在运行时由 `~/.codex-accounts/` 扫描得出 + `default`，无默认值 |

`resume` / `stop` / `status` **不收 `--account`**：账号从任务元数据**查出来**，不是默认值。
`status` 省略任务名 = 列出所有隔离目录下的全部任务。

### 不提供的参数（刻意的）

`--timeout`、`--background`、`-o`、`--log`、`--model`、`--sandbox` —— 这些要么会造成误用，
要么由工具派生。模型固定 `gpt-6-astra`，难度只由 `--effort` 分档。

### run 的行为

1. 校验参数 → 2. 校验／创建隔离目录（§6）→ 3. 建 `tasks/`、`reports/`、`logs/` →
4. 查重任务名 → 5. 读 brief，**自动前置**「不得使用任何 skill，除非本 brief 明确指定。」
   并打印一行提示（不做隐形魔法）→ 6. 起 codex，`stdin=DEVNULL`，输出 tee 到屏幕和
   `logs/<任务>.log` → 7. 从 log 头提取 session id 写进元数据 →
8. **进程结束后自己跑一遍判据（§8），打印结论，退出码＝判据结论**。

第 8 步是核心收益：`exit 1 ≠ 失败`这条最反直觉的知识被彻底消化，
harness 的完成通知里直接带成败结论，happy path 下无需再敲 `status`。

### 固定的 codex 参数

```
run:    codex exec --cd <abs> -m gpt-6-astra -c model_reasoning_effort=<E>
        --sandbox danger-full-access -c approval_policy=never -c project_doc_max_bytes=0
        --skip-git-repo-check --disable plugins -o <D>/reports/<N>.json <brief>
resume: codex exec --cd <abs> resume <session_id> …同上，但
        -c sandbox_mode=danger-full-access   # resume 不认 --sandbox（退出码 2）
```
环境：`CODEX_HOME=<D>`、`CODEX_SQLITE_HOME=~/.codex`（会话索引共享，resume 才找得到）。
`--cd` 必须在 `resume` **之前**（放后面 clap 直接拒收）。

## 6. 隔离目录不变量（每次 run/resume 都校验）

| 不变量 | 违反时 |
|---|---|
| `config.toml` 是普通文件，**不是软链** | 拒跑并说明：软链主配置＝隔离失效 |
| `config.toml` 内容为工具写入的安全基线 | 缺失则创建 |
| `auth.json` 软链指向该账号登录态 | 缺失则创建；账号无登录态则拒跑 |
| `skills/`、`plugins/` 存在 | 缺失则创建（空） |

## 7. 任务元数据 `tasks/<任务>.json`

```json
{"task": "...", "account": "...", "dir": "/abs/...", "effort": "low",
 "session_id": "01a0…", "started_at": "...", "pid": 254153}
```

存在的理由：`resume` 必须回到**同一个目录、同一个会话**。有元数据就不用重新给 `--dir`，
也就不可能 resume 到错的目录去。它同时是 `status` 无参时的任务清单来源。

## 8. 判据（`run` 收尾与 `status` 共用一个纯函数）

| 状态 | 条件 |
|---|---|
| `running` | 真实 PID 存活 |
| `success` | 报告存在且非空 + log 末 50 行无**未知**运行时 ERROR |
| `suspect` | 报告存在且非空 + 有未知运行时 ERROR → 打印那几行，交给人判断 |
| `failed` | 报告缺失或为空（＝没正常收尾），`reason` 说明为何 |

### 真实 PID 怎么拿（不能靠 `$!`、不能靠日志）

`pgrep -f "reports/<任务>.json"` → 再按 `ps -o comm=` 是 `codex` 收窄 → `kill -0` 确认。
报告路径在 argv 里且按任务唯一，这是「按任务命名」的第二个用处。
`$!` 拿到的是包装链最外层，不是 codex（2026-09-17 实测：`$!`=254151，codex=254153）。

### 运行时 ERROR 怎么认（2026-09-19 实测，决定性）

log 里**混着 brief 原文和 codex 转述的子进程输出**，裸 `grep -E 'ERROR|WARN'` 大面积误报。
实测出现过的误报源：cargo 的 `error[E0599]`、pytest 的 `E   KeyError`、
brief 里引用 TDD skill 的 `**Test errors?**`、markdown 标题 `## Warning Signs`。

判据必须是：**剥掉 ANSI 转义后，行首为 `ERROR:` 或 `WARN:`**（codex 自己的运行时前缀）。

已知良性（过滤掉，不计入）：
- `failed to refresh available models`
- `Reconnecting... waiting for network`（可恢复，后续正常继续）

特判进 `reason`（不新增状态）：`You've hit your usage limit` —— 补救手段不同（换账号／等额度）。

### 不进判据的东西

原 skill 的 `jq -e '.commit // .branch'` **属于任务层，不属于工具层**——那是 brief 要求
codex 填的字段。工具只管「有没有正常收尾」，并把报告的顶层 key 列出来给调用方自己看。

## 9. 续跑与停止

| 命令 | 硬约束 |
|---|---|
| `resume N` | 先跑判据，状态是 `running` 就**拒绝**（对还在跑的会话 resume，报错和 SIGTERM 锁死一模一样，处置却相反：一个该等，一个该弃） |
| `stop N` | **只发 `SIGINT`**，只发给本任务的真实 PID。永不裸 `kill`（SIGTERM 会让 thread 永久锁死、再也 resume 不了），永不 `pkill -f`（共享机器会误杀，且模式会匹配到发命令的 shell 自己） |

## 10. 测试

| 层 | 内容 | 成本 |
|---|---|---|
| 单测（stdlib `unittest`） | 参数校验、隔离目录不变量（含 config.toml 软链拒绝）、判据函数（用真实 log 片段做 fixture：cargo error／pytest E／markdown 标题 **不得**判成 ERROR；`ERROR: Reconnecting` 判良性；`ERROR: You've hit your usage limit` 判 reason）、argv 组装、session id 提取（带 ANSI） | 零 token |
| 端到端冒烟 | 一次 `--effort low` 的真任务，验证起→收尾→判据→resume 全链路 | 一次低档调用 |

## 11. 迁移

1. 实现 `codex_agent.py` + 单测，全绿。
2. 改写 `SKILL.md` 到 ~40 行：派什么任务、effort 分档表、四条命令。
   250 行里的实测教训（日期／当时怎么炸的）**搬进代码注释，贴在防住它的那行旁边**。
3. 合回 master，目录 `codex-sub-agent` → `codex-agent` 改名，建 `~/.local/bin/codex-agent` 软链。
4. 端到端冒烟通过后，删除本 spec 与计划文档。
