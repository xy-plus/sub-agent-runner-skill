# sub-agent-runner：改名 + 加 DeepSeek runner

## 目标

同一个工具既能派 **codex 子代理**，也能派 **DeepSeek 子代理**（`claude-deepseek -p`），
两侧共用同一套任务名、判据、报告和打断/续跑协议。

用户原话：
- 「现在这个 skill 只能用 Codex，我希望它能够使用 DeepSeek，具体的命令是 `claude-deepseek -p`……
  但是这个 skill 就得改名字了」
- 「我现在正在用原生的 Cloud Code，希望可以调用 Codex 子代理，也可以调用 DeepSeek 子代理。
  但是，这都是通过 skill 来完成的。它是运行在外部的，走不了原生。」
- 「如果是 DeepSeek 的话，它的 effort 必须要是 max。」
- 名字定为 **`sub-agent-runner`**；skill 白名单两侧都要支持

> **v2（2026-09-23）**：v1 被一个 **DeepSeek 子代理**审查退回（43 轮、$3.36）。
> 它找到 9 条必改，我核实后接受 8 条。最重要的一条是 v1 完全没看见的：
> **日志里的两个标记文本含 `codex-sub-agent`，它们是盘上契约**——
> 全局改名会让所有现存日志的轮次边界和打断痕迹当场认不出来。
> 顺带：那次审查自己的日志跑到了 **12 MB**，正是本 spec 第一节那条 58 KB/s 的现场证据。

## 一、全部设计依据都是实测

`claude-deepseek` 是把 Claude Code 指向 DeepSeek 端点的**透传包装**（`exec` 掉自己），
白名单里只有 `deepseek-flash[1m]` 一个模型。2026-09-23 实跑得出：

| 机制 | codex | deepseek | 怎么测的 |
|---|---|---|---|
| 隔离 | `CODEX_HOME` | **`CLAUDE_CONFIG_DIR`** | 指向空目录照跑；auth 走环境变量，不在配置目录里 |
| 工作目录 | argv 里的 `--cd` | **`Popen(cwd=…)`** | `claude -p` 没有 `--cd` 等价物；实测设了 `cwd` 它就在那里干活 |
| 权限 | `--sandbox danger-full-access` + `approval_policy="never"` | **`--dangerously-skip-permissions`** | 不给的话写入全被拒（见下），给了之后 denials=0、文件真落盘 |
| 报告 | `-o <文件>` | **`result` 事件的 `result` 字段** | `--output-format stream-json`（强制要 `--verbose`，不带退 1） |
| session id | 从日志正则抠 | **工具自己指定** `--session-id <uuid>` | 指定的 uuid 原样回传；`--resume <同一个>` 上下文完整 |
| 续跑 | `codex exec resume <id>` | `--resume <uuid>` | **被 SIGINT 打断之后照样接得上** |
| skill 白名单 | brief 前面给 SKILL.md 绝对路径 | **同样的做法** | 哨兵实验：给路径→拿到暗号；不给→「不知道」 |
| 成败 | 从产物和日志推 | **`is_error` / `subtype`** | `success` / `error_during_execution` |
| 进程身份 | `comm=="codex"` | **`comm=="claude"`**，且 `Popen.pid` 就是它 | 读 `/proc/<pid>/comm` 实测；`claude-deepseek` 是 `exec`，没有多一层包装 |

### 三条会咬人的实测

**① 不给权限开关，子代理一个字都写不成，而这一轮仍然报 `success`。**

```
subtype: success   is_error: False   退出码 0
permission_denials: [Write 被拒, Bash 被拒, Bash 被拒]
工作目录里实际有: []
```

这正是 `--skill` 那次立项要杀的「**零工作量的成功**」。两道防线都要有：

- `--dangerously-skip-permissions`（和 codex 侧那两个开关对称）
- **`permission_denials` 非空时，runner 往日志写一行 `ERROR: …`** ——
  于是 `judge` 判 `suspect`（退 3，「干完了但要人看一眼」），用的是现有状态机，零新分支

**② SIGINT 之后退出码是 0**，和成功一样——退出码区分不了。但 `result` 事件的 `subtype`
是 `error_during_execution`，结构上分得开。（codex 那侧退出码同样不可信，这是两侧一致的老规矩。）

**③ `thinking_tokens` 事件每 token 一条**：8 秒 2320 条、462 KB，约 **58 KB/s**。
本次 spec 审查那一个子代理的日志跑到了 **12 MB**。没有任何开关能关掉它。
另：进程被杀时 JSONL 最后一行必然是残的（实测），解析必须容忍。

## 二、改名：三样东西不许跟着改

`codex-sub-agent` → **`sub-agent-runner`**。改的是**目录名、文件名、命令名、文档**。

| 不许改 | 为什么 |
|---|---|
| **隔离目录名** `~/.codex-subagent*` | 它是 260 份现存元数据里的归属。改名会让那些任务从 `status`/`resume`/`stop` 里全部消失 |
| **`ROUND_MARK`** = `"===== codex-sub-agent "` | **盘上契约**。它写在每一份日志里，`_ROUND_LINE` 整行匹配它。改掉之后 `read_last_round` 在旧日志上找不到轮次起点，会把整份日志当成这一轮——前几轮的错误全算进来 |
| **`INTERRUPT_MARK`** = `"----- codex-sub-agent 本轮被 INT 打断……"` | 同上。改掉之后盘上已有的打断痕迹认不出，`interrupted` 退回 `failed`——正是代码里记着那个 28k token 的坑 |

**一次全局 `sed` 会同时踩中后两条。** 改名必须逐处判断：是「工具的名字」还是「盘上的契约」。

不对称是刻意的：**用户敲的是命令名，盘上的东西谁都不该动。**

## 三、判据：一套，不是两套

> **deepseek runner 只在 `subtype == "success"` 时才把 `result` 写进报告文件。**

`judge` 现有那条「报告没出现＝没正常收尾」**原样生效**——一套判据、零新状态。

打断同理：`subtype == "error_during_execution"` 时不写报告，而日志里有工具自己写的打断标记，
于是判 `interrupted`／退 130，和 codex 侧逐字一致。

**runner 的职责边界就此确定**：把这一轮的产物摆成 `judge` 认识的形状
（报告文件 + 本轮日志文本），`judge` 本身一行不改。

### 失败和受阻时 `detail` 里要有东西

`judge` 的 `detail` 来自 `runtime_error_lines`，认的是 `^ERROR:` / tracing / `^Error:`。
JSONL 每行以 `{` 开头，**一条都不会命中**——不补的话 deepseek 失败时 `detail` 是空的。

修法**不是**给 judge 加分支，而是 **runner 往日志写现有分类器原样认得的行**：

| 情形 | runner 写什么 | judge 给出 |
|---|---|---|
| `subtype != "success"` | `ERROR: <subtype>: <首行信息>` | `failed`（报告没写） |
| `permission_denials` 非空 | `ERROR: 本轮有 N 次工具调用被拒：<工具名…>` | 成功时是 `suspect`（报告在 + 有错误行） |

**代价要写明：** 除上面这两种由 runner 自己产生的行之外，deepseek 侧的 `errors` 恒为空，
所以「codex 中途已恢复的工具错误」那类 `suspect` 在 deepseek 侧**结构性不可达**。
这是接受的代价，不是「判据两侧都生效」。

## 四、runner 是什么：七件事，不是三件

v1 写成「只有三个方法的接口」，而代码里真正需要按 runner 分叉的点有七个。
**写不全，实现者就会在 `run_codex` 和五个 `cmd_*` 里散落 `if runner ==`**——正是「必须记得」那类。

| # | 分叉点 | codex | deepseek |
|---|---|---|---|
| 1 | 隔离目录 | `~/.codex-subagent[-账号]` | `~/.claude-subagent` |
| 2 | 隔离不变量 | 五个子目录 + 共享扫描根为空 + `config.toml` 不是软链 + `auth.json` 软链 | **只建目录**（`tasks`/`reports`/`logs`）。共享扫描根那条是 codex 专属 |
| 3 | 环境 + 工作目录 | `CODEX_HOME`；工作目录走 argv 的 `--cd` | `CLAUDE_CONFIG_DIR`；**工作目录走 `Popen(cwd=)`** |
| 4 | 起跑 argv | `codex exec --cd … -o <报告> <brief>` | `-p --effort max --output-format stream-json --verbose --dangerously-skip-permissions --session-id <uuid> <brief>` |
| 5 | 续跑 argv | `codex exec resume <id> …` | `--resume <uuid> …` |
| 6 | tee 与收尾 | 分块读；从头部抠 session id；报告由 codex 自己写 | **行模式**；滤掉 `thinking_tokens`；解析末条 `result` 事件；成功才写报告；必要时补 `ERROR:` 行 |
| 7 | PID 反查的 needle | 报告路径（`-o` 的那个 argv 元素） | session id uuid |

**第 9 节列出了每一个必须改的调用点**，实现时照着它走，别自己找。

### uuid 必须是独立的 argv 元素

`--session-id <uuid>` 和 `--resume <uuid>` **都不许写成 `=` 连接**。
PID 反查按 argv 元素**精确比对**（不是正则——codex 侧那条注释记了实测事故：
任务 `a` 的报告路径拿去 pgrep，命中了任务 `aXmd`）。写成 `--session-id=<uuid>` 就成了
一个元素，精确匹配静默失配，于是 `status` 说不在跑、`stop` 不发信号、`run` 放行第二轮
——两个写者共写一份日志。**要有对称的测试钉住。**

### session id 归谁：两侧最大的不对称，而 deepseek 那半更对

codex 的 session id 是从日志正则抠出来的，而日志混着 brief 原文和子进程输出。
deepseek 的由工具生成、落进元数据、此后每轮从元数据读——**绝不从输出里读回来核对**
（那会把一个已知事实变成待验证的推测）。

`codex exec` 没有这个参数（它自己生成），所以 codex 侧改不了。
**不对称是外部约束造成的，不是设计选择。**

### 任务名复用时 uuid 怎么办

**复用任务名 = 新一轮 = 新 uuid。** `run` 的语义两侧一致：新会话、无上下文。
沿用旧 uuid 会让 `run` 悄悄变成 `resume`，而调用方看不出来。

## 五、CLI

`--runner codex|deepseek` **必填**，不给缺省。

三条硬拒绝，一律退 2 并说清**为什么**，不静默改正——`claude-deepseek` 自己就是这么干的
（它的注释：「静默跑错模型变成大声退 2」）：

| 写了什么 | 结果 |
|---|---|
| `--runner deepseek --account <任何值>` | 拒：这个 runner 没有账号概念（一个 DeepSeek token） |
| `--runner deepseek --effort` 不是 `max` | 拒：这个 runner 只接受 `max` |
| `--runner codex` 不给 `--account` | 拒（现有行为不变） |

**这三条必须跑在 `cmd_run` 任何状态变更之前**，尤其在那段**会删旧元数据的迁移分支**之前。
代码里已有同款教条（`check_can_resume`：「凡是对已交进来输入的纯检查，一律排在不可逆动作之前」）。
排在后面的话，一次被拒的调用已经把一个任务从 `status`/`resume`/`stop` 里抹掉了。

`--effort` 两侧都**必填**，deepseek 侧只有 `max` 一个合法值。
**理由是「统一 CLI 形状」**——调用方只记一个模板，不必记「哪个 runner 要不要这个参数」。
（不是「不许有缺省值」的推论；那条推不出这一条。）

`--skill` / `--no-skill` 两侧完全一致，`prepend_skill_guard` 一行不改。

`resume` / `stop` / `status` / `interrupt-and-resume` **不收 `--runner`**——从元数据查出来。

## 六、隔离目录

| runner | 目录 | 要建的子目录 |
|---|---|---|
| codex | `~/.codex-subagent`／`~/.codex-subagent-<账号>` | `skills`/`plugins`/`tasks`/`reports`/`logs`（不变） |
| deepseek | `~/.claude-subagent` | **`tasks`/`reports`/`logs`** |

`tasks`/`reports`/`logs` 少一个就是 tee 循环里的 `FileNotFoundError`——包装器中途死掉、
子进程变孤儿。代码里那条注释记着 codex 侧为此连踩两次。

deepseek 侧**没有账号维度**（一个 token），`auth.json` 软链和 `config.toml` 那两条不适用，
共享扫描根 `~/.agents/skills` 非空就拒跑那条是 **codex 专属**（那是 codex 的扫描根）。

## 七、元数据：加 `runner`，并一次性迁移现存的 260 个

| 字段 | codex | deepseek |
|---|---|---|
| `runner` | `"codex"` | `"deepseek"` |
| `account` | 账号名（不变） | **`None`** |

`account` 对 deepseek **必须是 `None`，不许记一个假名字**：它进 `status` 第二列，
而且 `_resume_round` 会拿它去 `ensure_isolation`——记 `"deepseek"` 会指向
`~/.codex-subagent-deepseek` 这个不存在的账号目录并因缺 auth 拒跑。

`isolation_home` 的签名从 `(account)` 改成 **`(runner, account)`**：
「住在哪个隔离目录」由一对值决定，拆成两个参数各传各的正是「好 API 难被误用」要消掉的缝。

**`find_meta` / `all_metas` 的扫描根要加上 deepseek 那一个。** 它们现在只扫
`account_choices()` × `isolation_home(account)`，而 `~/.claude-subagent` 不对应任何账号
——不加的话 deepseek 任务对 `status`/`resume`/`stop` **整体不可见**。

**迁移：** 实测现存 260 份（`~/.codex-subagent` 49、`-acct2` 187、`-acct3` 24），
**没有一个还在跑**。加字段**之前**跑一次脚本补上 `"runner": "codex"`，用 `write_meta` 原子替换，
补完核对数量。**这是一次操作，不是一段代码**——代码里不留任何「读不到就当 codex」的路径
（`REQUIRED_META_KEYS` 是从 `new_meta` 派生的，特判它等于破坏那个不变量；写缺省又撞铁律 6）。

## 八、测试的两道护栏

1. **`setUpModule` 的「不许真起 codex」断言要扩到 `claude-deepseek`。**
   现在它只挡 `argv0 == "codex"`。加了第二个 runner 之后，漏包一层 mock 的测试会
   **真的把 DeepSeek 叫起来**——花钱、发网络请求。
2. **改名的工作量不是零**：`find_codex_pid` 在两个文件里有几十处引用，
   测试里还有钉死 `f"codex-sub-agent {cmd}"` 的断言。计划里要给它留出量。

## 九、必须改的调用点清单（照着走，别自己找）

| 位置 | 改什么 |
|---|---|
| `isolation_home` | 签名 `(runner, account)`；deepseek 分支；**目录名不动** |
| `new_meta` | 加 `runner`；deepseek 的 `account` 必须 `None`；校验写在构造器里 |
| `REQUIRED_META_KEYS` | 跟着构造器走，不另写清单 |
| `find_meta` / `all_metas` | 扫描根加上 deepseek 的家 |
| `ensure_isolation` | 按 runner 分叉不变量；deepseek 只建三个子目录 |
| `auth_source` | 只对 codex 有意义，加断言 |
| `find_codex_pid` → `find_agent_pid` | `comm` 与 needle 按 runner 取；**保留「argv 元素精确比对、绝不用正则」** |
| `run_codex` | `Popen` 加 `cwd=`；tee 按 runner 选分块／行模式；收尾调 runner 的钩子 |
| `_tee_until_exit` | 加行模式分支；**codex 那一支一个字节不动** |
| `build_parser` | `--runner` 必填；`--account` 改可选 |
| `cmd_run` | 三条硬拒绝，排在任何状态变更之前 |
| `cmd_resume` / `_resume_round` | 从元数据取 runner；`check_can_resume` 第一道闸的注释要拆（那段 4.06 秒论证是 codex 专属） |
| `cmd_status` | 多一列或复用第二列显示 runner |
| `setUpModule`（测试） | 断言扩到 `claude-deepseek` |

## 验收判据

1. **改名**：命令是 `sub-agent-runner`；**盘上契约一个都没改**——
   `~/.codex-subagent*` 原样、`ROUND_MARK` 与 `INTERRUPT_MARK` 的文本**一字未动**，
   且有测试钉住这两个字面量
2. **三条硬拒绝**：各自退 2 且错误信息说清为什么；**且都发生在任何状态变更之前**
   （构造一个「任务原属别的账号 + 参数非法」的场景，断言旧元数据还在）
3. **真跑一次要产出真文件**：`--runner deepseek` 的冒烟必须在 `--dir` 里创建一个文件并核对内容
   ——**只断言「报告非空」挡不住「零工作量的成功」**
4. **权限受阻要变红**：`permission_denials` 非空时日志有 `ERROR:` 行，`judge` 给 `suspect`／退 3
5. **工作目录**：deepseek 的产物落在 `--dir` 里，不在包装器的 cwd 里
6. **判据**：`success` → 写报告 → judge `success`；`error_during_execution` → 不写报告 →
   有打断标记时 `interrupted`／退 130
7. **日志**：`thinking_tokens` 被滤掉（滤在 tee）、日志开头写明滤过、
   **codex 那条 tee 路径一个字节没改**、尾部坏行不许让解析崩
8. **uuid 是独立 argv 元素**：`--session-id`／`--resume` 两处都有测试钉住不是 `=` 连接
9. **PID 反查**：打断之后 `status` **真的**变 `interrupted`（进程确实停了），
   而不是只留下痕迹；不相干的 claude 进程不许命中
10. **元数据**：新任务记 `runner`；deepseek 的 `account` 是 `None`；
    **260 份现存元数据全部迁移完毕**且 `status` 一个不少；
    代码里**没有**任何「读不到 runner 就当 codex」的路径
11. **deepseek 任务查得到**：`status`／`resume`／`stop` 对它们全部有效
12. **测试护栏**：`setUpModule` 的断言挡得住 `claude-deepseek`；既有测试全绿；
    **codex 侧行为一字未变**
13. **收尾**：结论内化进注释后删除 spec 与计划文档
