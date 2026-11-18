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
号都有未到期的限流记录时，`run` **在任何任务状态变更和代理启动之前拒绝，
退出码 2**，逐个列出账号与恢复时间。新任务先用 Codex；全满被拒后按 `SKILL.md`
不等恢复，换个任务名显式用 `--runner deepseek` 顶上（用户要求，2026-10-23）。
中途撞限仍以退出码 1 结束，**不自动重跑**，因为那一轮可能已改过文件。
看过工作目录、必要时补好 brief 后，重发同一条命令即可重新选号；全满仍被拒，
显式账号名仍只用该号。同一产物目录内，同一个任务名在任何模式下都不能换 runner；
auto 只在 codex 账号之间选号，换号时更新元数据里的账号；任务文件仍在原产物目录，
`run` 开新会话、不带旧上下文。

两侧共用同一套任务名、判据、报告和打断／续跑协议：`--runner` 决定派给谁，
其余四条命令都从元数据查出来，不用再报一遍。

```bash
OUTPUT_DIR="${RUNS_ROOT:?RUNS_ROOT must be set}/-home-xy-repo/20261024-120000-runner"
mkdir -p "$OUTPUT_DIR"
# 把任务说明写进 "$OUTPUT_DIR/brief.md" 后启动：
sub-agent-runner run --task <任务名> --runner codex    --output-dir "$OUTPUT_DIR" --dir /abs/repo --brief "$OUTPUT_DIR/brief.md" \
                --effort max --account auto --no-skill
sub-agent-runner run --task <任务名> --runner deepseek --output-dir "$OUTPUT_DIR" --dir /abs/repo --brief "$OUTPUT_DIR/brief.md" \
                --effort max --no-skill
sub-agent-runner status [任务名] --output-dir "$OUTPUT_DIR"
sub-agent-runner resume <任务名> --output-dir "$OUTPUT_DIR" --brief "$OUTPUT_DIR/follow.md" --effort max --no-skill
sub-agent-runner interrupt-and-resume <任务名> --output-dir "$OUTPUT_DIR" --brief "$OUTPUT_DIR/msg.md" --effort max --no-skill
sub-agent-runner stop <任务名> --output-dir "$OUTPUT_DIR"
```

五条命令都给同一个 `--output-dir`：已经存在的
`<RUNS_ROOT>/<以 - 开头的项目目录名>/<年月日-时分秒>-<主题>/`。
项目目录名直接取本会话记录在 `~/.claude/projects/` 下所在的目录名，示例里的
`-home-xy-repo` 和时间主题按本次事情替换。`--dir` 仍是实际工作的仓库或工作树。
`RUNS_ROOT` 必须已设，路径可以写 `~` 或相对路径，入口会解析为绝对路径。

任务按「产物目录 + 任务名」定位；`status` 省略任务名时只列这个目录里的任务。
元数据、报告、日志分别在 `sub-agent-runner/tasks/`、`sub-agent-runner/reports/`、
`sub-agent-runner/logs/`，brief 和 agent 自己的文件也放在这件事目录中，
工具名子目录归对应工具，因此可以和 `xy-goal/` 契约共处。账号目录只管登录、
配置、缓存与会话记录。

旧任务由合入编排者排空旧包装器和两个后端的子代理后，一次性整体搬迁；本工具
不在运行时迁移。搬完后管理旧任务，`--output-dir` 给
`<RUNS_ROOT>/<编排者项目目录名>/<年月日-时分秒>-sub-agent-runner-legacy-<账号>/`，
不传其中的 `sub-agent-runner/`。元数据字段和内容无需为搬迁改写。

## 安装

```bash
git clone <this repo> ~/.claude/skills/sub-agent-runner
ln -sfn ~/.claude/skills/sub-agent-runner/sub_agent_runner.py ~/.local/bin/sub-agent-runner
```

仓库须装在 `~/.claude/skills/sub-agent-runner`。在全局 `~/.claude/settings.json` 的
`env` 中指定 `RUNS_ROOT`，并把下面的 Bash hook 项合并到 `hooks.PreToolUse`，
保留已有的其他环境变量和 hook 配置：

```json
{
  "env": {
    "RUNS_ROOT": "/data/user/xy/runs"
  },
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

隔离目录名 `~/.codex-subagent*`、`~/.claude-subagent` 保留，用于各账号的运行环境。
旧日志里的轮次分隔符和打断标记仍可读取，任务元数据字段不变。

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

整仓测试也可用 `python3 -m unittest discover`。与新版 xy-goal 的联调用例需显式指定
脚本路径（未指定时跳过，不把旧布局当成联调通过）：

```bash
XY_GOAL_SCRIPT=/abs/xy-skills/xy-goal/goal.py python3 -m unittest test_sub_agent_runner.TestGoalRunsIntegration -v
```

设计上的取舍和踩过的坑都写在代码注释里，贴在防住它的那行旁边——
`test_sub_agent_runner.py` 的模块 docstring 是维护者入口，那里记着这个仓库
在「空测试」上栽过的八种形态和它们的解药。

**测试一次都不会真起 codex 或 claude**：`setUpModule` 拦截本进程的启动，并在 PATH
最前面放置临时替身，让 Python、Bash 等子进程也继承防线。替身记下命令后以非零
退出；模块收尾发现记录就令测试失败，防止误调用被正常的失败用例吞掉。
加第三个 runner 时那份可执行名清单要跟着加，否则那条护栏对它是空的。
