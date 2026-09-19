# 进程身份 = PID + 启动时刻；等待用 pidfd

日期：2026-09-20　状态：第五版（按 v4 审查重写），待二轮审查

判据层级：**三条铁律 > 仓库规范 > 既有文档**。

## 版本小史

| 版本 | 做法 | 否掉的理由 |
|---|---|---|
| v1 | 日志里写「收尾标记」+ 文本匹配 | 用户：拿给人读的日志当 IPC 信道 |
| v2/v3 | 日志文件加排他锁 | 锁的释放挂在 stdout 管道上，而 stdout 刻意给全部后代——**病根被继承进新答案**；且拿锁要提前 open 日志，`tell()` 快照过期 |
| v4 | PID + 启动时刻（用户提出） | 机制对，**但判据写错两处**，其中一处是 v3 病根的同一形状 |

**四版共同的教训**：挡住我的从来不是问题难，是一条我自己立下、之后再没审视过的约束
（「不存 PID」——它防的是**裸** PID，我当成了全面禁令）。

## 1. 病根

续跑之前必须确认**没有人还会往这份日志里写字**。现在等的是「codex 从 `/proc` 消失」，
而真正在写的是**包装器**：tee 循环读 codex 的 stdout **管道**，codex 的后代继承了它，
codex 死了管道不 EOF。实测复刻：

```
t=0.000s  codex=zombie  writer=alive    ← 排干中，日志还在长
t=2.987s  codex=gone    writer=alive    ← 仅持续 20ms
t=3.007s  codex=gone    writer=gone
```

## 2. 身份：PID + 启动时刻

`/proc/<pid>/stat` 第 22 字段是启动时刻。PID + 启动时刻唯一确定一个进程。
实测（PID namespace + `ns_last_pid` 强制复用到同一个 PID）：裸 `os.kill(pid,0)` 说「活着」，
比对启动时刻则正确判 `gone`。

**解析**：`comm` 字段可含空格、括号、制表符、**裸换行**（实测全部出现过），
必须**整文件读 bytes**、从**最后一个 `)`** 之后切；**禁止按行读**（要有突变钉住）。

**类型定为 `str`**：json 往返不保类型，实测「存 int 比 str」会把活着的任务**静默判死**，
而且是全部任务一起。`_load_meta` 要校验类型，不只校验存在。

### 唯一性的余量：44 万倍，不是真问题

启动时刻的分辨率是 10ms（`CLK_TCK=100`）。要让两个进程的 (PID, 启动时刻) 撞车，
必须在**同一个 10ms 刻度内**把同一个 PID 发两次——而 PID 是顺序分配、绕 `pid_max` 才回头：

| | 本机实测 |
|---|---|
| `pid_max` | 4,194,304 |
| 要在 10ms 内绕完一圈 | 需 **4.19 亿次 fork/秒** |
| 实际 fork 速率 | **942 次/秒**（绕一圈 1.2 小时） |
| **余量** | **445,255 倍** |

**所以这不是一个需要处理的问题，一条注释即可，零行代码。**

（审查用 PID namespace + 直接写 `ns_last_pid` 强制构造过撞车，那是实验室手法，
自然情况下发生不了。唯一真实的边界是：**别拿这套身份去判一个活不到 10ms 的短命进程**
——本工具记身份的只有 codex 和包装器，都是分钟到小时量级。这句写进注释就够了。）

## 3. 三个命名谓词，不暴露三态字符串

僵尸在两处**语义相反**：codex 僵尸 = 「别发信号、也别算写完」；writer 僵尸 = 「已经写完了」。
把三态裸字符串交给 6 个调用点，等于把这张映射表交给记忆——而它只活在一份**即将被删**的 md 里。

```
_process_state(pid, start) -> "alive" | "zombie" | "gone"      # 私有
codex_should_be_signaled(meta) -> bool    # codex alive（僵尸不发：kill 成功但无效）
task_is_busy(meta)            -> bool     # writer alive **or** codex alive
log_writer_finished(meta)     -> bool     # writer != alive（僵尸也算写完）
```

每个谓词的语义、以及「僵尸算哪边」，写在它自己的注释里。

### `log_writer_finished` 为什么是 `!= alive` 而不是 `gone`

writer 退出但未被回收时是 `zombie`——**fd 早已全部关闭，一个字都不会再写**。
而「何时被回收」由 harness 的 bash/node 决定，**是本工具管不着的第三方**。
等 `gone` 就是把正确性挂在第三方身上——**v3 被否的同一形状**。

### `task_is_busy` 为什么两个都要看

- 只看 codex：排干期间它已是僵尸 → `cmd_run` 放行第二个 run → **同一份日志两个写者**。
  而 `run_codex` 里 `tell()` 快照那四条前提的第 1 条原文就是「`cmd_run` 撞见同名任务还在跑
  就拒绝，所以不会有两个 run 共写一份日志」——当场失效。
- 只看 writer：实测 SIGKILL 掉 writer 之后 **codex 存活并跑完**（孤儿 codex），会漏判。

调用点共 **5 处**：`cmd_run` 查重、`cmd_resume` 的闸、`cmd_status`、`cmd_stop`、
`cmd_interrupt_and_resume`。v4 漏了 `cmd_resume`。

## 4. 等待：用 `pidfd_open`，不轮询

v4 写「等的是别的进程，没有内核唤醒机制」——**实测为假**。
`os.pidfd_open` + `select` 在**非子进程**上正常唤醒（实测 t=2.002s 精确醒），
而且**在「终止」那一刻可读、不等父进程回收**，对僵尸 `pidfd_open` 也成功并立即可读。

于是一次性删掉：轮询循环、间隔常量、以及 `gone`/`zombie` 的歧义
——**pidfd 可读 == 停止运行 == 停止写日志**，正是要问的那个谓词。

PID + 启动时刻仍然需要：它是**能存进 json 的**那半边身份。
`pidfd_open` 之后**再校一次启动时刻**，闭合「open 之前 PID 就被复用」的 TOCTOU。

## 5. 超时是策略值，不是实测上界

v4 写「锚点 3.07/3.08s」——**那不是一个有上界的量**：排干多久由 codex 起的后代决定，
起个后台服务就永不结束。代码里同一现象记的是 4.16s，spec §1 记的是 3.08s，同一负载两个数。

所以：超时 = **本工具愿意等多久的策略值**，保留 60s，注释写明
「**这不是排干时长的上界，排干无上界**」。超时文案给下面那条诊断。

### 三种成因分开说

| 成因 | 怎么判 | 说什么 |
|---|---|---|
| 正常排干 | writer alive、codex **zombie** | 等几秒就好 |
| 同名任务真在跑 | codex alive | 换个任务名，或先 stop |
| 后代占着管道 | **超时后**仍 writer alive、codex zombie | codex 已退出，但包装器还在读输出——很可能它起的后台进程还占着输出管道 |

**行 1 与行 3 是同一观测，只按「超时前/超时后」分**（v4 把行 3 写成 `codex gone`，实测不可达：
排干期间 codex 恒为僵尸，`(gone, alive)` 只存在 20ms）。写明这一点，
否则实现成一个 `classify()` 就永远分不出来。

## 6. 元数据分两次写

`writer_pid` / `writer_start` = `os.getpid()`，**Popen 之前就知道** → 进第一次 `write_meta`。
`codex_pid` / `codex_start` → Popen 之后第二次写。

这样中间崩掉的失败面从「两个 writer 同写一份日志」降级成「这一轮发不了信号」
——因为 `task_is_busy` 靠 writer 身份就已经成立。

现有那段「`write_meta`/`clear_report` 排在 spawn 之前」的十行顺序论证要**重写**，不是照抄。

**`REQUIRED_META_KEYS` 加四个字段的爆炸半径**：`all_metas()` 逐个走 `_load_meta`，
一个坏 json 让不带任务名的 `status` 整条退 2。代码注释要求「加字段前先确认没有在跑的任务」，
已代查：`~/.codex-subagent`、`-acct2`、`-acct3` 各 **0 个 json**，今天加字段影响为零。
**这个复核结果写进 spec，并注明「那是当时的事实，不是永久豁免」。**

## 7. 四者对齐要改的地方（v4 一条没提）

| 位置 | 要改什么 |
|---|---|
| `test_codex_agent.py` 模块 docstring | 承重约束清单里「进程反查的三道过滤 uid/comm/argv」「EPERM 即存活」「元数据字段清单」三条 |
| `OWNED_BY_CODE` | `r"/proc\|pgrep\|pkill" → "find_codex_pid"` 指向一个将不存在的函数 |
| `interrupt_codex` docstring | 「由调用方用 `find_codex_pid` 判」 |
| `run_codex` 的 `tell()` 四条前提 | 第 1 条 |
| §1 的 `0.04s/3.08s` vs 代码注释的 `0.03s/4.16s` | 同一事实两个家，留一个 |

## 8. 实体账（不拿它当正确性论据）

实测行数：删 `pid_alive`(11) + `find_codex_pid` 含论证(53) + `wait_for_exit`(29)
+ 超时常量块(12) + `new_meta` 的「刻意没有 pid」(2) = **107 行**；
测试删 `TestPid`(141) + `TestWaitForExit`(42) = **183 行**。

但 `wait_for_exit` 和常量块是**改写不是删除**，回填约 37 行 → 实现净删 **≈70 行**。

**加的**（v4 漏记了大半）：身份采集函数、三个命名谓词、第二次 `write_meta` 及其顺序论证、
`WRITER_EXIT_TIMEOUT`、5 个调用点各自的转换、以及 §9 那些真进程夹具（僵尸/强制复用/排干复刻，
审查复刻各花 30~60 行）。

**结论：实现大致打平，测试净增。** 四版里这笔账错了四次——**从此不拿它当论据**。

## 9. 测试

| 测什么 | 关键点 |
|---|---|
| 身份挡得住 PID 复用 | PID 相同、启动时刻不同 → `gone` |
| 僵尸判得出 | 真造僵尸（子退父不 wait）→ `zombie` |
| `comm` 含空格/括号/制表符/**裸换行** | 仍解析正确；**突变：改成按行读 → 必须红** |
| starttime 类型 | 存 int 比 str → 必须被 `_load_meta` 拒，不能静默判死 |
| **codex 早没了、包装器还在排干时必须继续等** | 本次回归锁。突变：换回只看 codex → 必须红 |
| `task_is_busy` 两个都要看 | 突变：只看 codex → 红；只看 writer → 红（孤儿 codex 场景） |
| `log_writer_finished` 用 `!= alive` | 突变：改成 `gone` → 红（writer 僵尸场景） |
| pidfd 在终止那一刻醒 | 不等回收；对僵尸也可读 |
| pidfd 之后再校 starttime | 突变：去掉复校 → 红 |
| 三种成因各自的拒绝语 | 按 §5 那张表逐条 |
| 身份缺失 | `_process_state(None, None)` **不许**吞成 `gone`，必须显式拒绝 |

依赖外部进程的测试**先断言前提成立**（本仓栽过七次）。

## 10. 非目标

- `judge`、`read_*`、`interrupt_codex` 的行为不动。
- 不改日志格式、不引入锁、不引入任何文本标记。
