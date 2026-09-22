---
name: codex-sub-agent
description: 把有明确规划的执行类任务（照 DoD 写代码、对抗审查、e2e 验证）派给 codex 后台跑，省 claude token。用 Bash run_in_background 启动 run。
---

# codex-sub-agent

**本 skill 一旦激活，默认就不再开 claude 子代理了**——执行类的活交给 codex，
claude 只编排。同时开两边等于白烧 claude 的 token，那正是这个 skill 要省的东西。

把**有明确规划的执行类任务**派给 codex 干。Prompt 写进 `brief.md`。

命令怎么拼、隔离目录怎么建、进程怎么判活、成败怎么判、信号怎么发，全部由
`codex_sub_agent.py` 保证，本文件只讲**人／模型才能决定的事**。

## 启动：一个任务 = 一次 `Bash(run_in_background: true)`

```bash
codex-sub-agent run --task <任务名> --dir /abs/repo --brief brief.md --effort <档位> --account auto --no-skill
```

五个带值参数**全必填**，外加 `--skill`/`--no-skill` 二选一，没有默认值。
`run_in_background: true` 是唯一正确的启动方式——harness 追踪它、面板可监控、
**完成时的通知里直接带成败结论**。
绝不 `nohup … &`：detach 之后就只剩存活、没有通知。

## 难度靠 effort 分档，不靠换模型

`gpt-6-astra` 只有这五档（2026-09 查证，`minimal` 和 `ultra` 是别的模型的）。

| 档 | 什么时候用 |
|---|---|
| `low` | 照一份已经写死的 DoD 改代码、批量重构、跑闸收集结果——**确认是纯机械劳动才降到这** |
| `medium` | 要读一段陌生代码再改、线索明确的 debug |
| **`high`** | **默认档。没有明确理由降档，就用它** |
| `xhigh` | 根因不明的 bug、要推翻前提的设计 |
| `max` | 没有已知解法，判据得自己找 |

**不要拿成本当降档的理由。** 五档之间的价差只有约 4 倍，而档位不够导致的返工要
搭进整整一轮——省下的那点远不够赔。降档的唯一正当理由是「这活真的不需要判断」。

## 另外四条命令

```bash
codex-sub-agent status [任务名]        # 省略则列出全部
codex-sub-agent resume <任务名> --brief follow.md --effort <档位> --no-skill
codex-sub-agent interrupt-and-resume <任务名> --brief msg.md --effort <档位> --no-skill
codex-sub-agent stop <任务名>          # 上下文保留，之后还能 resume
```

`status` 不带任务名时**一行一个任务**：前四列（任务名／账号／状态／退出码）都不含空格，
第五段起是工作目录和一句人话；**缩进的行是明细，不是任务**。

`codex exec` **没有收件箱**——给正在跑的那一轮塞消息是做不到的。要给它新信息，
只有 `interrupt-and-resume` 一条路，而它＝**打断当前轮**。

**要不要为此打断，看这条信息值不值。** 工具替不了这个判断：它要知道「这条信息
值多少」和「在途工作损失多少」，后者在 codex 里根本不可观测。
已做的部分**留在上下文里不会白做**（实测：打断后续跑，追问它被打断前成功建了
哪几个文件，它自己答得出，磁盘上也确实只有那几个），但当前这一轮的收尾会没有。

## 调用方仍需要知道的四件事

| 事 | 内容 |
|---|---|
| 退出码 | `0` success、`1` failed、`3` suspect（干完了，但本轮日志有未分类的 codex 错误，要人看一眼）、`4` running、`130` interrupted（被打断，**接着续跑即可，不要重跑**——130 就是 Ctrl-C 那个既成约定，脚本作者不读本文档也认得）、`2` 参数写错或被护栏拒绝。**看数字，不看词**：完成通知对任何非零码都写 `failed with exit code N`，「failed」这个词消不掉，能区分的只有那个数字 |
| brief | 只收**文件路径**，不收内联字符串。**skill 禁令那句话归工具所有，不要自己写进 brief**——写了当场拒跑 |
| 账号 | `--account auto` —— **默认就写这个**：工具自己挑一个没在限流的。撞上额度上限**不会自动重跑**——实测额度永远是在任务跑到一半用完的，那一轮多半已经改过文件了，重跑会落在一棵改了一半的树上。它会记下恢复时间并失败，**重跑这条命令就会自动换号**。**一个账号可以同时开多个子代理**，不用排队等前一个跑完。要盯住某一个账号就写它的名字（`default` / `acct2` / `acct3`，由 `~/.codex-accounts/` 扫出来，加一个账号就自动认），那样撞上限只记录、**不换号** |
| 给它 skill | `--skill <SKILL.md 的绝对路径>`，可重复；一个都不给就 `--no-skill`。**路径写错当场拒跑**，不会再静默跑出一个「零工作量的成功」。它给的是**许可**——工具会叫 codex 动手前先读一遍，但**什么时候按它办事仍要 brief 自己说**。不要往共享目录里放东西 |

codex 不靠谱 → 换 claude 子代理。
