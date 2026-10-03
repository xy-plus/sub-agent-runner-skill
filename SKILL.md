---
name: sub-agent-runner
description: 把任务派给 codex 或 DeepSeek 子代理后台跑，省 claude token。用 Bash run_in_background 启动 run。
---

# sub-agent-runner

**本 skill 一旦激活，默认就不再开 claude 原生子代理了**，
改用本 skill 调用其他 agent，Prompt 写进 `brief.md`。

命令怎么拼、隔离目录怎么建、进程怎么判活、成败怎么判、信号怎么发，全部由
`sub_agent_runner.py` 保证，本文件只讲**人／模型才能决定的事**。

## 启动：一个任务 = 一次 `Bash(run_in_background: true)`

```bash
sub-agent-runner run --task <任务名> --runner codex    --dir /abs/repo --brief brief.md --effort max --account auto --no-skill
sub-agent-runner run --task <任务名> --runner deepseek --dir /abs/repo --brief brief.md --effort max     --no-skill
```

带值参数**一个都别省**（`--account` 例外：它只有 `codex` 要，`deepseek` 给了反而被拒，
见下表），外加 `--skill`/`--no-skill` 二选一，没有默认值。
`run_in_background: true` 是唯一正确的启动方式——harness 追踪它、面板可监控、
**完成时的通知里直接带成败结论**。
绝不 `nohup … &`：detach 之后就只剩存活、没有通知。

## 派给谁跑：`--runner`

| runner | 跑的是什么 | 账号 | effort |
|---|---|---|---|
| `codex` | `codex exec`，`gpt-6-luna` | `--account` 必填 | **只有 `max`** |
| `deepseek` | `claude-deepseek -p`，`deepseek-flash[1m]`（1M 上下文） | **不收 `--account`**（只有一个 token） | **只有 `max`** |

两侧的任务名、判据、报告、退出码、打断／续跑协议完全一样；
`status`／`resume`／`stop`／`interrupt-and-resume` **都不收 `--runner`**（从元数据查出来）。
组合写错当场退 2 并说清为什么，**不会替你改正**。

**同一个任务名不能换 runner**：会话、报告、日志都在原来那一侧，换个任务名。

**新任务一律 `--runner codex`**；`deepseek` 只留给已经在跑的任务查状态、续跑（用户 2026-10-03：「后续只使用codex……不用deepseek，已经启动的不用停」）。

## 模型与 effort 都定死

codex 侧只用 `gpt-6-luna`，effort 只有 `max`；deepseek 侧也只有 `max`（用户 2026-10-03：「要求只用gpt6 luna max」）。
两者都写死在 `sub_agent_runner.py` 里（`MODEL`、`EFFORTS`），传别的档位当场退 2，不会替你改正。

## 另外四条命令

```bash
sub-agent-runner status [任务名]        # 省略则列出全部
sub-agent-runner resume <任务名> --brief follow.md --effort max --no-skill
sub-agent-runner interrupt-and-resume <任务名> --brief msg.md --effort max --no-skill
sub-agent-runner stop <任务名>          # 上下文保留，之后还能 resume
```

`status` 不带任务名时**一行一个任务**：前四列（任务名／派给谁跑／状态／退出码）都不含空格，
第五段起是工作目录和一句人话；**缩进的行是明细，不是任务**。
第二列 codex 的写成 `codex:acct2`，deepseek 的就是 `deepseek`。

**两侧都没有收件箱**——给正在跑的那一轮塞消息是做不到的。要给它新信息，
只有 `interrupt-and-resume` 一条路，而它＝**打断当前轮**。

**要不要为此打断，看这条信息值不值。** 工具替不了这个判断：它要知道「这条信息
值多少」和「在途工作损失多少」，后者在子代理里根本不可观测。
已做的部分**留在上下文里不会白做**（实测：打断后续跑，追问它被打断前成功建了
哪几个文件，它自己答得出，磁盘上也确实只有那几个），但当前这一轮的收尾会没有。

## 调用方仍需要知道的四件事

| 事 | 内容 |
|---|---|
| 退出码 | `0` success、`1` failed、`3` suspect（干完了，但本轮日志有未分类的子代理错误，要人看一眼；deepseek 侧这条只会来自「有工具调用被拒」）、`4` running、`130` interrupted（被打断，**接着续跑即可，不要重跑**——130 就是 Ctrl-C 那个既成约定，脚本作者不读本文档也认得）、`2` 参数写错或被护栏拒绝。**看数字，不看词**：完成通知对任何非零码都写 `failed with exit code N`，「failed」这个词消不掉，能区分的只有那个数字 |
| brief | 只收**文件路径**，不收内联字符串。**skill 禁令那句话归工具所有，不要自己写进 brief**——写了当场拒跑 |
| 账号 | **只有 `--runner codex` 有账号。** `--account auto` —— **默认就写这个**：工具自己挑一个没在限流的。撞上额度上限**不会自动重跑**——实测额度永远是在任务跑到一半用完的，那一轮多半已经改过文件了，重跑会落在一棵改了一半的树上。它会记下恢复时间并失败，**重跑这条命令就会自动换号**。**一个账号可以同时开多个子代理**，不用排队等前一个跑完——但 **`auto` 不是调度器**：同时起的两条 `auto` 看到的排序一模一样，会挑到同一个账号然后一起撞上限，要分散就各自写死账号名。要盯住某一个账号就写它的名字（`default` / `acct2` / `acct3`，由 `~/.codex-accounts/` 扫出来，加一个账号就自动认），那样撞上限只记录、**不换号** |
| 给它 skill | **两侧写法完全一样。** `--skill <SKILL.md 的绝对路径>`，可重复；一个都不给就 `--no-skill`。**路径写错当场拒跑**，不会再静默跑出一个「零工作量的成功」。它给的是**许可**——工具会叫 codex 动手前先读一遍，但**什么时候按它办事仍要 brief 自己说**。不要往共享目录里放东西 |

## deepseek 这一侧还要知道两件事

它的日志是 JSONL，而且**滤掉了 `thinking_tokens` 进度事件**——那东西每 token 一条，
实测约 58 KB/s，一次审查的日志跑到 12 MB。日志开头那行写明了滤掉的是什么，其余一字未改。

它的错误行只有工具自己补的两类（**这一轮没成功**、**有工具调用被拒**），
所以 codex 那种「中途已恢复的工具错误」式 `suspect` 在这一侧不会出现；
反过来，一旦看到 `3`（suspect），就是「它收尾了但有工具被拒，报告不可尽信」。

两个 runner 都不靠谱 → 换 claude 原生子代理。
