# sub-agent-runner：改名 + 加 DeepSeek runner

## 目标

同一个工具既能派 **codex 子代理**，也能派 **DeepSeek 子代理**（`claude-deepseek -p`），
两侧共用同一套任务名、判据、报告和打断/续跑协议。

用户原话：
- 「现在这个 skill 只能用 Codex，我希望它能够使用 DeepSeek，具体的命令是 `claude-deepseek -p`，
  这样就可以增加一个选 model 的能力了，但是这个 skill 就得改名字了」
- 「我现在正在用原生的 Cloud Code，希望可以调用 Codex 子代理，也可以调用 DeepSeek 子代理。
  但是，这都是通过 skill 来完成的。它是运行在外部的，走不了原生。」
- 「如果是 DeepSeek 的话，它的 effort 必须要是 max。」
- 名字：**`sub-agent-runner`**（用户定）
- skill 白名单：两侧都要支持（用户选）

## 一、全部设计依据都是实测，不是设想

2026-09-23 对 `claude-deepseek`（它是把 Claude Code 指向 DeepSeek 端点的一层透传包装，
白名单里只有 `deepseek-flash[1m]` 一个模型）实跑得出：

| 机制 | codex | deepseek | 怎么测的 |
|---|---|---|---|
| 隔离 | `CODEX_HOME` | **`CLAUDE_CONFIG_DIR`** | 指向空目录照跑；auth 走环境变量，不在配置目录里 |
| 报告 | `-o <文件>` | **`result` 事件的 `result` 字段** | `--output-format stream-json` 最后一条 `result` 事件带齐 |
| session id | 从日志正则抠 | **工具自己指定**（`--session-id <uuid>`） | 指定的 uuid 原样回传；之后 `--resume <同一个 uuid>` 拿回了上下文 |
| 续跑 | `codex exec resume <id>` | **`--resume <id>`** | session id 不变，上下文真接上；**被 SIGINT 打断之后照样能接上** |
| skill 白名单 | 在 brief 前面给 SKILL.md 绝对路径 | **同样的做法就行** | 哨兵 skill 实验：给路径→拿到暗号；不给→「不知道」 |
| 成败 | 只能从产物和日志推 | **`is_error` / `subtype` 结构化** | `success` / `error_during_execution` |

两条会咬人的：

1. **SIGINT 之后退出码是 0**，和成功一样——**退出码区分不了打断**。
   但 `result` 事件的 `subtype` 是 `error_during_execution`，结构上分得开。
   （codex 那侧的退出码同样不可信，这条是两侧一致的老规矩。）
2. **`thinking_tokens` 事件每 token 一条**：8 秒 2320 条、462 KB，约 **58 KB/s**。
   长任务能把日志涨到几十 MB，而判据要扫日志（实测 0.83 MB 约 7.6 ms，线性放大）。
3. **进程被杀时 JSONL 最后一行是残的**（实测 1 行坏行）。解析必须容忍尾部坏行。

## 二、改名

`codex-sub-agent` → **`sub-agent-runner`**。改的是**目录名、命令名、文档**。

**隔离目录一律不改名。** `~/.codex-subagent*` 原样保留，DeepSeek 侧新开 `~/.claude-subagent`。
理由：隔离目录名写在每一份任务元数据的归属里，改它会让现有每一个任务从
`status`／`resume`／`stop` 里消失——正是这个仓库最忌讳的静默失效。
而目录名是内部的，用户敲的是命令名。**不对称是刻意的，写进注释。**

## 三、判据：一套，不是两套

DeepSeek 给了结构化的成败，诱惑是为它单开一条判据。**不要。**

> **deepseek runner 只在 `subtype == "success"` 时才把 `result` 写进报告文件。**

这样 `judge` 现有那条「报告没出现＝没正常收尾」**原样生效**——一套判据、零新状态、
而结构化信号在它能用的地方被用上了。

打断同理：`subtype == "error_during_execution"` 时不写报告，而本轮日志里有工具自己写的
打断标记（`interrupt_codex` 写的，两侧共用），于是 `judge` 判 `interrupted`／退 130，
和 codex 侧逐字一致。

**runner 的职责边界就此确定**：把「这一轮的产物」摆成 `judge` 认识的形状
（报告文件 + 本轮日志文本），judge 本身一行不改。

## 四、CLI

`--runner codex|deepseek` **必填**，不给缺省（仓库规范第 6 条）。

三条硬拒绝，一律退 2 并说明原因，**不静默改正**——`claude-deepseek` 自己就是这么干的
（模型名不在白名单就退 2，它的注释写「静默跑错模型变成大声退 2」）：

| 写了什么 | 结果 |
|---|---|
| `--runner deepseek --account <任何值>` | 拒：这个 runner 没有账号概念（一个 DeepSeek token） |
| `--runner deepseek --effort` 不是 `max` | 拒：这个 runner 只接受 `max`（用户的硬约束） |
| `--runner codex` 不给 `--account` | 拒（现有行为，不变） |

`--effort` 两侧都**必填**：deepseek 侧只有 `max` 一个合法值，但仍要显式写出来。
写死成隐式 max 就是缺省值，而且调用方不会知道自己用的是哪档。

`--skill` / `--no-skill` 两侧**完全一致**，`prepend_skill_guard` 一行不改。

`resume` / `stop` / `status` / `interrupt-and-resume` **不收 `--runner`**——
runner 从元数据查出来，和 `account` 同一个模式，不可能指错。

## 五、每个 runner 各自要做的三件事

runner 是一个**只有三个方法的接口**，不是一个类层级：

| 方法 | codex | deepseek |
|---|---|---|
| 起跑的 argv 和环境 | `codex exec --cd … -m … -c effort … --sandbox … -o <报告> <brief>` + `CODEX_HOME` | `claude-deepseek -p --effort max --output-format stream-json --verbose --session-id <uuid> <brief>` + `CLAUDE_CONFIG_DIR` |
| 续跑的 argv | `codex exec resume <id> …` | `--resume <uuid> …` |
| 收尾：决定要不要写报告 | 不做（报告由 codex 的 `-o` 自己写）；顺带从日志抠 session id | `subtype=="success"` 时把 `result` 写进报告；**session id 不用抠，是工具自己给的** |
| **PID 反查的判据** | `comm=="codex"` 且**报告路径**是独立 argv 元素 | `comm=="claude"` 且**那个 uuid** 是独立 argv 元素 |

### session id 归谁：这是两侧最大的不对称，而且 deepseek 那半更对

codex 侧的 session id 是**从日志正则抠出来的**——而日志正是混着 brief 原文和子进程输出的那个东西。
deepseek 侧可以用 `--session-id` **由工具自己指定**（实测：指定的 uuid 原样回传，之后 `--resume`
拿回了上下文）。于是：

- **少一条从文本里捞事实的路**（这个仓库反复在修的正是这一类）
- **PID 反查有了着力点**：`find_codex_pid` 现有两条判据里，「报告路径是独立 argv 元素」是
  codex 专属的——`claude -p` 的 argv 里**根本没有报告路径**。uuid 顶上这个位置，
  两侧的反查判据在形状上完全对称：`comm` + 一个工具自己拥有的唯一串

uuid 在任务创建时生成、落进元数据，此后每一轮都从元数据读。**不从输出里读回来核对**——
那会把一个已知事实变成一个待验证的推测。

**日志过滤也归 runner**：deepseek 侧在 tee 的时候丢掉 `thinking_tokens` 事件
（58 KB/s，长任务会把日志涨到几十 MB）。丢弃是**有损**的，所以要在日志开头写明
「本轮日志已滤掉 thinking_tokens 进度事件」——不然读日志的人会以为工具漏记了。

## 六、隔离目录

| runner | 目录 |
|---|---|
| codex | `~/.codex-subagent`（default）／`~/.codex-subagent-<账号>` |
| deepseek | `~/.claude-subagent` |

deepseek 只有一个 token，所以只有一个目录，**没有账号维度**。
`ensure_isolation` 的不变量要按 runner 分叉：codex 侧查 `auth.json` 软链和 `config.toml`，
deepseek 侧只需要建目录（auth 走环境变量，配置目录空着正是我们要的隔离）。

共享扫描根 `~/.agents/skills` 非空就拒跑这条是 **codex 专属**的（那是 codex 的扫描根），
deepseek 侧不适用。

## 七、刻意不做

| 不做 | 理由 |
|---|---|
| 用 Claude Code 原生的 `--bg` | 它会引入第二套生命周期（session id、`claude attach/logs/stop/rm`），而这个工具的全部价值就是**一套统一的判据**。用 `-p` 复用现有 spawn/tee/judge |
| 把隔离目录改名统一 | 见第二节，会让现有任务全部消失 |
| 给 deepseek 加多账号 | 它只有一个 token。没有可分配的东西 |
| 为 deepseek 单开一条判据 | 见第三节 |
| `--model` 这个参数名 | 换的是整个后端（隔离机制、报告怎么拿、有没有账号都不一样），叫 `--runner` 才是实话 |
| 迁移现有任务的元数据 | 现有任务没有 `runner` 字段。**读不到就当 codex**——这是唯一一处向后兼容的缺省，理由是它对应的是「这条元数据写于只有 codex 的年代」这个事实，不是一个被省略的选择 |
| 给 codex 侧也改成工具指定 session id | `codex exec` 没有这个参数（它自己生成）。**不对称是外部约束造成的，不是设计选择**，写进注释免得下一个人以为是疏忽 |

## 验收判据

1. **改名**：命令是 `sub-agent-runner`；`~/.claude/skills/sub-agent-runner/` 存在；
   `settings.json` 等处的引用已更新；`SKILL.md`／`README.md` 与实际一致；
   **隔离目录名一个都没改**（`~/.codex-subagent*` 原样）
2. **硬拒绝**：`--runner deepseek --account X` 退 2 并说明；
   `--runner deepseek --effort high` 退 2 并说明；两条错误信息都要说清**为什么**
3. **两侧共用**：同一份 brief、同一个 `--skill`，两个 runner 都能跑通；
   `prepend_skill_guard` 一行未改
4. **deepseek 的判据**：`subtype=="success"` → 写报告 → judge 判 success；
   `subtype=="error_during_execution"` → 不写报告 → 有打断标记时判 interrupted／退 130
5. **日志**：`thinking_tokens` 被滤掉，且日志开头写明滤过；
   **尾部坏行不许让解析崩**（实测被杀时必然产生一行残行）
6. **续跑**：deepseek 侧 `resume` 和 `interrupt-and-resume` 都能接上上下文
   （实测打断后 resume 可用）
7. **元数据**：新任务记 `runner`；**旧元数据没有这个字段时当 codex**，且 `status` 不崩
8. **PID 反查两侧都准**：`status`／`stop`／「还在跑」的闸在 deepseek 侧同样有效；
   反查判据按 runner 分叉，且**不许用正则**（codex 侧那条注释记了实测事故：
   任务 `a` 的报告路径拿去 pgrep 命中了任务 `aXmd`）
9. **既有测试全绿**，且 codex 侧行为一字未变
10. **收尾**：结论内化进注释后删除 spec 与计划文档
