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

`cmd_interrupt_and_resume` 有**两处**进程判断，各用各的：

| 位置 | 用谁 | 为什么 |
|---|---|---|
| 决定要不要发 INT | `find_codex_pid` | 要的是 pid，而且僵尸不该发 |
| 续跑之前等 | **两个都停** | 只等 writer 会撞上孤儿 codex 的写锁——**正是这条命令存在的理由** |

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

## 4. 等待用 pidfd，三条契约

`os.pidfd_open` + `select` 在非子进程上正常唤醒，**在「终止」那一刻可读、不等回收**，
对僵尸也成立（实测 Linux 6.8.0 / Python 3.12）。

**三条契约必须写进代码**：

1. **复校启动时刻必须排在任何阻塞等待之前。** 实测写反的代价：在一个无关的复用者上
   卡满整个超时（探针里 3.01s，真实代码是 60s）。
2. **复校不匹配就 `close(fd)` 且不等。**
3. **TOCTOU 闭合的理由是「open 之后才发生的复用，蕴含目标已被回收，而『不是 alive』本来就是
   正确答案」——不是 fd 锁住了 PID 号。** 实测：持有 pidfd **并不**锁住 PID，
   老 fd 仍正确指向老进程，而 `/proc/<pid>` 已经是新进程的。

**可用性前提**：内核 ≥5.3、Python ≥3.9。`os.pidfd_open` 的**属性存在是编译期性质、
`ENOSYS` 是运行期性质**，`hasattr` 挡不住 → 失败时给**一句读得懂的拒绝**，不是 traceback。

**pidfd 不做权限过滤**（实测能 open root 的 PID 1）。今天 `find_codex_pid` 的 uid 过滤是承重的
（本机有别的用户在跑 codex）——**codex 那侧保留该过滤**，writer 这侧顶上来的是启动时刻比对。
这条替换关系要写下来，否则下一个人会以为 uid 过滤是白留的。

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
- 不改日志格式、不引入锁、不引入任何文本标记。
- 不为 10ms 刻度做任何机制。
