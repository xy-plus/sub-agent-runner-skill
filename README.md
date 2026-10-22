# sub-agent-runner

一个 Claude Code skill：把**有明确规划的执行类任务**派给 **codex 子代理**（`codex exec`）
或 **DeepSeek 子代理**（`claude-deepseek -p`）在后台跑，claude 只做编排。

## 它解决什么

两个 agent 各有一圈实测出来的约束，必须每次记对：codex 这边 `--cd` 要绝对路径、
`</dev/null` 不能漏、只能 `kill -INT` 不能裸 `kill`、退出码 1 不等于失败、
`resume` 收的参数比主线少三个；DeepSeek 那边工作目录只能走 `cwd`（`claude -p` 没有
`--cd`）、不给 `--dangerously-skip-permissions` 会跑出一个**一个字都没写成的
「成功」**、`stream-json` 不带 `--verbose` 直接退 1……
**原来这些写在文档里，靠每次读一遍记住。**

DeepSeek 的 token 和模型由 `claude-deepseek` 每次生成的私有档案经 settings 的 env
注入 Claude Code 进程，档案的 env 压过继承来的模型变量；本工具原样透传环境，
只设置 `CLAUDE_CONFIG_DIR` 来隔离配置目录。

这个项目把它们编译成了一个可执行文件：调用方只提供任务本身的信息
（干什么、在哪干、多难），其余由代码保证。写错的参数**当场拒跑**，
而不是静默跑出一个「零工作量的成功」。

撞上账号额度上限也归它管：`--account auto` 会自己挑账号，所有有登录态的 codex
号都在限流期内时，**这一轮自动交给 DeepSeek**。中途撞限仍以退出码 1 结束，
**不自动重跑**，因为那一轮可能已改过文件。看过工作目录、必要时补好 brief 后，
重发同一条命令即可重新选号或交给 DeepSeek；显式账号名仍只用该号。

两侧共用同一套任务名、判据、报告和打断／续跑协议：`--runner` 决定派给谁，
其余四条命令都从元数据查出来，不用再报一遍。

```bash
sub-agent-runner run --task <任务名> --runner codex    --dir /abs/repo --brief brief.md \
                --effort max --account auto --no-skill
sub-agent-runner run --task <任务名> --runner deepseek --dir /abs/repo --brief brief.md \
                --effort max --no-skill
sub-agent-runner status [任务名]
sub-agent-runner resume <任务名> --brief follow.md --effort max --no-skill
sub-agent-runner interrupt-and-resume <任务名> --brief msg.md --effort max --no-skill
sub-agent-runner stop <任务名>
```

## 安装

```bash
git clone <this repo> ~/.claude/skills/sub-agent-runner
ln -sfn ~/.claude/skills/sub-agent-runner/sub_agent_runner.py ~/.local/bin/sub-agent-runner
```

仓库须装在 `~/.claude/skills/sub-agent-runner`。然后把下面的 Bash hook 项合并到
`~/.claude/settings.json` 的 `hooks.PreToolUse` 中，保留已有的其他配置：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/skills/sub-agent-runner/background_guard.py mark-call"
          }
        ]
      }
    ]
  }
}
```

在 Claude Code 里，用 `Bash(run_in_background: true)` 启动 `run`、`resume` 和
`interrupt-and-resume`。护栏会拒绝前台派活并显示正确写法；后台调用中用 `&`、
setsid、嵌套 shell 或循环脱离的 runner，会等到全部结束，把结论与退出码带进
这次调用的一次完成通知。只提到 runner 的 grep、printf 等命令照常运行。

这是防常见派活写法错误的护栏，以下四种刻意绕过不防：

- 用 `env -i` 或 `env -u SUB_AGENT_RUNNER_BASH_CALL` 清掉标记。
- 自设 EXIT trap 覆盖退出检查，或用 `exec` 换掉调用 shell。
- 命令文字里没有 `sub-agent-runner` 或 `sub_agent_runner` 名字的间接派活。
- 调用结束后才启动 runner。

护栏未注册或脚本失效时，runner 照常运行。安装路径不对会让每次 Bash 调用显示
hook error，护栏不起作用；已经开着的会话可能要重启才能拿到新注册的 hook。

需要 Python 3，外加你要用的那个 runner 的可执行文件：
[`codex`](https://github.com/openai/codex) CLI（`--runner codex`）／
`claude-deepseek`（`--runner deepseek`）。零第三方依赖。

**隔离目录名刻意不跟着改名**：`~/.codex-subagent*` 是现存任务元数据的归属，
日志里的轮次分隔符和打断标记同理（它们被整行正则匹配）。
改的只是命令名和文件名——用户敲的是命令名，盘上的东西谁都不该动。

同上：**改名／搬家时，旧命令的 symlink 必须同时废掉。** 只要旧命令还能跑，
它就还在按老格式写元数据，而少字段的任务会在 `status` 中报错、无法续跑
（见 `sub_agent_runner.py` 里 `REQUIRED_META_KEYS` 上方那段）。

## 怎么用

**看 [`SKILL.md`](SKILL.md)** —— 它是这个工具面向调用方的全部契约：五条命令、
参数、退出码、两个 runner 的差别，以及那些只有人/模型才能做的判断
（派给谁、派什么难度、要不要为一条消息打断当前轮）。

它刻意**不写**命令怎么拼、进程怎么判活、信号怎么发——那些由代码保证，
并且有一条测试（`TestSkillDocDoesNotRepeatCode`）守着「已由代码保证的约束不许在文档里重说」。

## 开发

```bash
python3 -m unittest test_sub_agent_runner -v
```

设计上的取舍和踩过的坑都写在代码注释里，贴在防住它的那行旁边——
`test_sub_agent_runner.py` 的模块 docstring 是维护者入口，那里记着这个仓库
在「空测试」上栽过的八种形态和它们的解药。

**测试一次都不会真起 codex 或 claude**：`setUpModule` 拦截本进程的启动，并在 PATH
最前面放置临时替身，让 Python、Bash 等子进程也继承防线。替身记下命令后以非零
退出；模块收尾发现记录就令测试失败，防止误调用被正常的失败用例吞掉。
加第三个 runner 时那份可执行名清单要跟着加，否则那条护栏对它是空的。
