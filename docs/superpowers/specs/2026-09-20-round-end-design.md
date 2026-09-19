# 只给 writer 一个身份，别的都别动

日期：2026-09-20　状态：第六版，待审查

判据层级：**三条铁律 > 仓库规范 > 既有文档**。

## 版本小史：同一个形状错了五次

每一版的修法都自带一个**没人保证的新前提**：

| 版本 | 做法 | 自带的新前提 | 谁保证 |
|---|---|---|---|
| v1 | 日志写「收尾标记」+ 文本匹配 | 日志文本恰好没有别人写 | 没人 |
| v2/v3 | 日志文件加排他锁 | 锁的释放 = stdout 管道 EOF | **stdout 刻意给全部后代** |
| v4 | PID+启动时刻，等 `gone` | harness 的 bash/node 何时回收 | **第三方** |
| v5 | 再存 codex 身份，分两次写 | **writer 活到第二次写完** | **那条链上唯一会死的进程** |
| v6 | 只记 writer 身份，pidfd 等待 | pidfd 可用（内核 ≥5.3 / Python ≥3.9） | 环境——**而且它一分钱没买到，见 §4** |

**第五次的病根说得最清楚**：v5 把「现场可观测的事实」（`/proc` 里有没有那个进程）
换成了「有人记得写下来的记录」，而那个「有人」就是 writer 自己。

**v6 的做法：只把 writer 的身份记下来，别的全部保持现状。**
`writer_pid` = `os.getpid()`，**Popen 之前就知道** → 一次 `write_meta` 写完 →
v5 的 §6、N1、N2 一起消失。

## 1. 病根（订正 v5 的说法）

续跑之前要确认**没有人还会往这份日志里写字**，而真正在写的是**包装器**：
tee 读 codex 的 stdout **管道**，codex 的后代继承了它，codex 死了管道不 EOF。

**v5 §1 说「现在等的是 codex 从 `/proc` 消失」——不准确。** 实测：僵尸的 `cmdline` 是空的，
而 `find_codex_pid` 用 argv 精确比对，所以今天的 `wait_for_exit` 在 codex
**变僵尸那一刻**就返回 True。排干还有好几秒。

```
t=0.000s  codex=zombie  writer=alive    ← wait_for_exit 此刻已返回 True，而日志还在长
t=3.007s  writer 才退出
```

## 2. 改动：加一个 writer 身份，等待条件变成「两个都停了」

```
元数据新增两个字段：writer_pid / writer_start        （str，一次写完）
writer_still_running(meta) -> bool                   （读 /proc 比对身份）
wait_until_quiet(meta, timeout)                      （pidfd 阻塞等）
```

**codex 那一侧一个字不改**：`find_codex_pid` 的 uid / comm / argv 三道过滤全部保留。
它是现场事实，writer 死了它照样说真话。

### 「僵尸算哪边」这个问题在 v6 里不存在

v5 花了一整节论证「僵尸在两处语义相反，所以要三个命名谓词」。实测发现**两边今天都已经免费做对了**：

| | 僵尸期的行为 | 谁做对的 |
|---|---|---|
| codex | `cmdline` 为空 → argv 比对拒绝 → 不发信号 | **今天的 argv 过滤**（无名、无测试的承重行为，要补注释 + 突变） |
| writer | pidfd 在**终止**那一刻可读，不等回收 | **pidfd 语义自带** |

所以谓词从 3 个降到 1 个，落盘字段从 4 个降到 2 个。

### 等待条件：两个都停了

| | |
|---|---|
| 还有人写日志吗 | `writer_still_running` |
| 会话还被占着吗 | `find_codex_pid`（孤儿 codex：SIGKILL 掉 writer 后 codex 会存活跑完，实测） |

### 等待折进 `run_codex`，不留给调用方

计划编写时发现一个 v6 留下的**真缺口**：`cmd_resume` 也只看 codex。
上一轮被打断、codex 已没、writer 还在排干时，`resume` 当场放行 → 新一轮的分隔符写进日志 →
**老 writer 的输出落在它后面** → `judge` 把上一轮的尾巴算成这一轮。
**同一类 bug，换了个入口。**

所以不要问「哪个命令该等」——**`run_codex` 是唯一的 spawn 入口，三条路都经过它**：

```
run_codex:
    读出**上一轮**元数据里的 writer 身份 → 等它停 + 等 codex 停
    → 写新元数据（含自己的身份）→ clear_report → 写分隔符 → spawn
```

调用方不可能忘，也不存在「哪个入口漏了」。

`cmd_interrupt_and_resume` 仍然要用 `find_codex_pid` 决定**要不要发 INT**（要的是 pid，
而且僵尸不该发）——那是另一件事，保留。

## 3. 身份：PID + 启动时刻

`/proc/<pid>/stat` 第 22 字段。实测（PID namespace 强制复用）：裸 `os.kill(pid,0)` 说「活着」，
比对启动时刻正确判「已退出」。

**解析**：`comm` 可含空格、括号、制表符、**裸换行**（实测都出现过）→ 必须**整文件读 bytes**、
从**最后一个 `)`** 之后切；**禁止按行读**（要有突变钉住）。

**类型定 `str`**：json 往返不保类型，实测「存 int 比 str」会把活着的任务**静默判死**。
`_load_meta` 要校验类型。pid 用到 `os.kill` 时要转 int（实测 `os.kill("123", …)` 直接 `TypeError`）。

### 10ms 刻度不是问题：余量 44 万倍

| | 本机实测 |
|---|---|
| 要撞同一刻度，得在 10ms 内绕完 `pid_max`(4,194,304) | 需 4.19 亿次 fork/秒 |
| 实际 fork 速率 | 942 次/秒 |
| **余量** | **445,255 倍** |

**一条注释，零行代码。** 唯一真实边界：别拿它判活不到 10ms 的短命进程——
本工具记身份的只有 writer，陪跑整轮。

## 4. 不用 pidfd，直接轮询

v6 用 `os.pidfd_open` + `select`。计划编写时算了一笔账，**它一分钱没买到**：

> codex 那半**没有记下来的身份**，只能轮询扫 `/proc`。所以整个等待函数的唤醒粒度
> **本来就被 `poll_interval` 钉死了**——pidfd 在 writer 那半最多买到一个轮询间隔，
> 代价是 `import select` + ENOSYS 拒绝路径 + 三条 TOCTOU 契约注释 + 3 条测试（约 40 行）。

所以：**直接轮询 `writer_still_running` + `find_codex_pid`，两个都停就往下走。**
一并删掉 v6 的内核版本前提、`hasattr` 挡不住 `ENOSYS` 那段、以及三条 TOCTOU 契约。

### 但 `writer_still_running` 必须同时看 state

v6 把「僵尸算写完了」交给 pidfd 语义。没有 pidfd 之后，**谓词自己要管**：
只比对启动时刻会把僵尸 writer 报成「还在写」，而它的 fd 早已全关。

```
文件不在 / 启动时刻对不上 / state == "Z"   → 停了
否则                                        → 还在写
```

多一行，配一条测试 + 突变。

## 5. 超时是策略值，按命令分开说

排干多久由 codex 起的后代决定，**无上界**（起个后台服务就永不结束）。
所以超时 = 本工具愿意等多久，不是实测上界。现有 `INTERRUPT_EXIT_TIMEOUT >= 1.854 * 30`
那条测试正把它当上界钉着，**要一起改**。

诊断按命令分：

| 命令 | 超时时说什么 |
|---|---|
| `cmd_run` / `cmd_resume` | 上一轮还在收尾；若 codex 也活着 → 换个任务名，或先 stop |
| `cmd_interrupt_and_resume` | codex 已退出，但包装器还在读输出——很可能它起的后台进程还占着输出管道。**不要再 stop**（已发过 INT，现有闸会挡第二发） |

## 6. 四者对齐：要改的地方（v5 只列了 5 处，实查 18+）

**本仓没有 README，doc 那一角就是 `SKILL.md`，实查干净**（`OWNED_BY_CODE` 的
`/proc|pgrep|pkill` 模式在其中无命中）→ 只需改 `OWNED_BY_CODE` 的值。

| 位置 | 要改什么 |
|---|---|
| `test_codex_agent.py` `TestMetaShape.FIELDS` 七字段**绝对值** + `assertNotIn("pid", FIELDS)` | **直接锁死本改动**，加字段必红；其注释「存下来的 PID 会过期、会被复用」是本改动的反命题 |
| 同文件 `test_run落盘的元数据键集_不多不少` 的注释 | 同一意图的第三个家 |
| `codex_agent.py` `new_meta` docstring「刻意没有 pid…」 | 反命题本尊——但**只反 codex 那半句**，writer 身份是新加的，要写清区别 |
| tee 循环里「**已登记的跟进项（尚未修）**：这个循环等的是 stdout 管道 EOF…」整段 | **这次就是在修它** |
| `INTERRUPT_EXIT_TIMEOUT == 60` **且** `>= 1.854 * 30` 两条断言 | 第二条把策略值当实测上界 |
| `TestInterruptAndResumeOrder` 里 `assert_called_with(..., INTERRUPT_EXIT_TIMEOUT, INTERRUPT_POLL_INTERVAL)` | 等待入口变了 |
| 任务名限制的历史注记「反查改成扫 `/proc`…见 `find_codex_pid`」 | `find_codex_pid` **保留**，这条不用改（v5 要改是因为 v5 要删它） |
| 模块 docstring 的「元数据原子替换——status 永远看不到半截 json」 | 一次写完，**不用加限定**（v5 分两次写才需要） |
| 约 32 处 `mock.patch.object(ca, "find_codex_pid", ...)` | **一处都不用动**（v6 不删它）——这是 v6 相对 v5 最大的省 |

## 7. 测试

| 测什么 | 关键点 |
|---|---|
| 身份挡得住 PID 复用 | PID 相同、启动时刻不同 → 不算 running |
| `comm` 含空格/括号/制表符/**裸换行** | 仍解析正确；**突变：改成按行读 → 必须红** |
| starttime 类型 | 存 int 比 str → 被 `_load_meta` 拒，不能静默判死 |
| **codex 早变僵尸、writer 还在排干时必须继续等** | 本次回归锁。突变：只等 codex → 必须红 |
| **孤儿 codex** | SIGKILL 掉 writer，codex 仍活 → 等待不许返回。突变：只等 writer → 必须红 |
| pidfd 复校排在阻塞之前 | 突变：写反 → 必须红（用小超时，别让测试自己挂死） |
| pidfd 不可用时的退化 | 给人话拒绝，不是 traceback |
| 僵尸 codex 不发信号 | 今天靠 argv 过滤免费做对，**补一条突变**把这条无名行为钉住 |
| 超时常量 | 绝对值钉住；**删掉「≥1.854×30」那条**（策略值不是上界） |

依赖外部进程的测试**先断言前提成立**（本仓栽过七次）。

## 8. 非目标

- **`find_codex_pid`、`pid_alive`、`interrupt_codex`、`judge`、`read_*` 一律不动。**
- 不改日志格式、不引入锁、不引入任何文本标记、**不引入 pidfd**。
- 不为 10ms 刻度做任何机制。
