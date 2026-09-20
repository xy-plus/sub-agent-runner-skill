# codex-sub-agent

一个 Claude Code skill：把**有明确规划的执行类任务**派给 `codex exec` 在后台跑，claude 只做编排。

## 它解决什么

`codex exec` 好用，但有一圈实测出来的约束必须每次记对：`--cd` 要绝对路径、
`</dev/null` 不能漏、只能 `kill -INT` 不能裸 `kill`、退出码 1 不等于失败、
`resume` 收的参数比主线少三个……**原来这些写在文档里，靠每次读一遍记住。**

这个项目把它们编译成了一个可执行文件：调用方只提供任务本身的信息
（干什么、在哪干、多难），其余由代码保证。写错的参数**当场拒跑**，
而不是静默跑出一个「零工作量的成功」。

```bash
codex-sub-agent run --task <任务名> --dir /abs/repo --brief brief.md \
                --effort high --account default --no-skill
codex-sub-agent status [任务名]
codex-sub-agent resume <任务名> --brief follow.md --effort high --no-skill
codex-sub-agent interrupt-and-resume <任务名> --brief msg.md --effort high --no-skill
codex-sub-agent stop <任务名>
```

## 安装

```bash
git clone <this repo> ~/.claude/skills/codex-sub-agent
ln -sfn ~/.claude/skills/codex-sub-agent/codex_sub_agent.py ~/.local/bin/codex-sub-agent
```

需要 Python 3 和 [`codex`](https://github.com/openai/codex) CLI。零第三方依赖。

## 怎么用

**看 [`SKILL.md`](SKILL.md)** —— 它是这个工具面向调用方的全部契约：五条命令、
参数、退出码、以及那些只有人/模型才能做的判断（派什么难度、要不要为一条消息打断当前轮）。

它刻意**不写**命令怎么拼、进程怎么判活、信号怎么发——那些由代码保证，
并且有一条测试（`TestSkillDocDoesNotRepeatCode`）守着「已由代码保证的约束不许在文档里重说」。

## 开发

```bash
python3 -m unittest test_codex_sub_agent -v
```

设计上的取舍和踩过的坑都写在代码注释里，贴在防住它的那行旁边——
`test_codex_sub_agent.py` 的模块 docstring 是维护者入口，那里记着这个仓库
在「空测试」上栽过的七种形态和它们的解药。
