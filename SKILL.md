---
name: codex-agent
description: 把有明确规划的执行类任务（照 DoD 写代码、对抗审查、e2e 验证）派给 codex 后台跑，省 claude token。用 Bash run_in_background 启动 run。
---

# codex-agent

把**有明确规划的执行类任务**派给 codex 干，claude 只编排。Prompt 写进 `brief.md`。

命令怎么拼、隔离目录怎么建、进程怎么判活、成败怎么判、信号怎么发，全部由
`codex_agent.py` 保证，本文件只讲**人／模型才能决定的事**。

## 启动：一个任务 = 一次 `Bash(run_in_background: true)`

```bash
codex-agent run --task <任务名> --dir /abs/repo --brief brief.md --effort low --account default
```

五个参数**全必填**，没有默认值。`run_in_background: true` 是唯一正确的启动方式——
harness 追踪它、面板可监控、**完成时的通知里直接带成败结论**。
绝不 `nohup … &`：detach 之后就只剩存活、没有通知。

## 难度靠 effort 分档，不靠换模型

| 档 | 派什么 |
|---|---|
| `low` | 确定性执行：照 DoD 改代码、批量重构、补测试、跑闸收集结果。**大多数任务到这一档就够** |
| `medium` | 要做取舍：读一段陌生代码再改、设计一个小接口、线索明确的 debug |
| `high` | 要权衡：跨模块改动、方案对比、对抗审查 |
| `xhigh` | 真难：根因不明的 bug、要推翻前提的设计 |
| `max` | 研究类：没有已知解法、判据要自己找 |

**默认从 `low` 起，不够再往上。** 一上来就 `max` 又贵又慢，而 `low` 这一档本身已经不弱——
用高档位换不来正确性，只换来更长的等待。

## 另外三条命令

```bash
codex-agent status [任务名]        # 省略则列出全部
codex-agent resume <任务名> --brief follow.md --effort low
codex-agent stop <任务名>          # 上下文保留，之后还能 resume
```

`codex exec` **没有收件箱**——给正在跑的那一轮塞消息是做不到的。要给它新信息，
只有「结束这一轮 + `resume`」一条路；要不要为此打断它，看信息本身值不值。

## 调用方仍需要知道的三件事

| 事 | 内容 |
|---|---|
| 退出码 | `0` success、`1` failed、`3` suspect（干完了，但本轮日志有未分类的 codex 错误，要人看一眼）、`4` running、`2` 参数写错或被护栏拒绝 |
| brief | 只收**文件路径**，不收内联字符串。工具会自动前置「不得使用任何 skill」并打印一行提示 |
| 给它 skill | 在 brief 里写该 skill 的**绝对路径**让 codex 自己读，不要往共享目录里放东西 |

codex 不靠谱 → 换 claude 子代理。
