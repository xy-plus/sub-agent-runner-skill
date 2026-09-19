---
name: codex-sub-agent
description: 把有明确规划的执行类任务（照 DoD 写代码、对抗审查、e2e 验证）交给 codex exec 后台跑，省 claude token。用 Bash run_in_background 启动以便监控。
---

# Codex Sub-Agent（`codex exec`）

把**有明确规划的执行类任务**派给 codex 干，claude 只编排。Prompt 写进 `brief.md`（避引号地狱）。

## 启动：一个任务 = 一次 `Bash(run_in_background:true)`

```bash
D=$HOME/.codex-subagent; mkdir -p "$D/reports"
CODEX_HOME=$D CODEX_SQLITE_HOME=$HOME/.codex \
codex exec --cd /abs/dir -m gpt-6-astra -c model_reasoning_effort="low" \
  --sandbox danger-full-access -c approval_policy="never" -c project_doc_max_bytes=0 \
  --skip-git-repo-check --disable plugins -o "$D/reports/<任务名>.json" "$(cat brief.md)" \
  > codex.log 2>&1 </dev/null
```

**`-o` 写在 `$D` 上而不是 `$CODEX_HOME` 上**，因为 `VAR=x cmd` 的赋值对**同一条命令
自己的参数不可见**——参数在赋值生效之前就展开完了，写 `$CODEX_HOME` 会展开成空。

**每个任务单独一次 Bash 工具、`run_in_background: true`**——harness 追踪它、面板可监控、完成自动通知。
**绝不 `nohup … &` 批量**：那样进程 detach，harness 追踪不到，只能手动轮询（要起多个就多调几次 Bash）。
命令末尾**不加 `&`**（`run_in_background` 已在后台；加了反而孤立进程）。

三条必守，每条都实测踩过：

- **`--cd` 必须绝对路径**——相对路径启动即崩（log 无 banner + `os error 2`）。
- **`</dev/null` 必带**——否则 codex 等 stdin 永久挂死。
- **不加 timeout**——会误杀正当的长任务。

## 难度靠 effort 分档，不靠换模型

只用 `gpt-6-astra` 一个模型，`model_reasoning_effort` 决定档位：

| 档 | 派什么 |
|---|---|
| `low` | 确定性执行：照 DoD 改代码、批量重构、补测试、跑闸收集结果。**大多数任务到这一档就够** |
| `medium` | 要做取舍：读一段陌生代码再改、设计一个小接口、线索明确的 debug |
| `high` | 要权衡：跨模块改动、方案对比、对抗审查 |
| `xhigh` | 真难：根因不明的 bug、要推翻前提的设计 |
| `max` | 研究类：没有已知解法、判据要自己找 |

**默认从 `low` 起，不够再往上。** 一上来就 `max` 又贵又慢，而 `low` 这一档本身已经不弱——
用高档位换不来正确性，只换来更长的等待。

## Skill/Plugin 隔离（防自激活）

`CODEX_HOME` 指向**专用最小目录**：`skills/` 只剩 codex 自带的几个内置小工具、`plugins/` 空、
无 MCP、无 hooks。用户装的 skill/plugin 对 codex **结构性不可见**。
`brief` 里再固定写一句「**不得使用任何 skill，除非本 brief 明确指定**」兜底。

**账号和隔离是正交的两件事，要组合、不要二选一。** `codex-acct <name>` 把 `CODEX_HOME` 指向
`~/.codex-accounts/<name>`，而那个目录的 `config.toml` **软链到主配置**——一用就把 MCP、
plugins、hooks、memories 全带回来，隔离就没了。正确做法是给每个账号一个隔离目录：

```bash
D=$HOME/.codex-subagent-<账号>
mkdir -p "$D/skills" "$D/plugins"
ln -sfn "$HOME/.codex-accounts/<账号>/auth.json" "$D/auth.json"   # 只软链登录态
cat > "$D/config.toml" <<'TOML'
model = "gpt-6-astra"
model_reasoning_effort = "medium"
approval_policy = "never"
sandbox_mode = "danger-full-access"
service_tier = "default"
TOML
```

**`config.toml` 必须是自己的文件，不许软链主配置。** 默认账号用 `$HOME/.codex-subagent`。
`CODEX_SQLITE_HOME=$HOME/.codex` 让会话索引共享，`resume` 才找得到。

**要传 skill 给它**：brief 里写明该 skill 的绝对路径（如 `~/.claude/skills/xxx/SKILL.md`），
让 codex 自己读取并遵循——不动共享目录，并行任务互不污染。
`~/.agents/skills/` 是 `CODEX_HOME` 管不到的共享扫描根（保持为空）：放了东西 codex 就看得见。

## 监控与收尾

- **「还活着吗」只有一个可靠判据：真实 PID。日志判不了、`$!` 给不出。**
  - 回合用尽的 codex 留下的 log 和还在跑的**长得一样**（末尾都是正常输出、没有收尾标记），
    所以 `tail codex.log` 只能看它在干什么，不能判存活。
  - 包装链里的 `$!` 拿到的是**最外层的包装**，不是 codex。2026-09-17 实测：
    `setsid nohup env … codex exec …` 的 `$!` 是 254151，而 codex 是 **254153**——
    据此连判两次「已退出」，又据此 `resume`，撞上它自己的写锁，两次都白费。
  - **PID 只从 `-o` 那个路径反查**：`pgrep -f "reports/<任务名>.json"`，
    再按 `ps -o comm=` 是 `codex` 收窄。那个字符串在 argv 里、且按任务唯一——
    **这是「按任务命名」的第二个用处**。判存活用 `kill -0 <PID>`，
    **不用 `pgrep -f <模式>`**：模式会匹配到发命令的 shell 自己。
- **存活和可观测是两件事，不能用同一个手段解决。**
  通知来自 harness 的追踪，不来自 codex：`run_in_background: true` 两样都给（见「启动」）；
  一旦 detach（`setsid` / `nohup` / `disown`）就**只剩存活**——进程活着干完了，没人告诉你。
  2026-09-17 因此空等两小时，而活早在两小时前就干完了。
  真需要 detach（比如怕被别的东西停掉），就**另起一个 harness 追踪的等待器**：
  `until ! kill -0 <真实 PID>; do sleep 30; done`，`run_in_background: true`。
  工作进程只管存活、等待器只管通知，**两者互不依赖**——等待器被停掉，重挂一个就行，
  工作一秒都没受影响。
- 收尾自述在 `-o` 指定的 `$CODEX_HOME/reports/<任务名>.json`（没出现＝没正常收尾）。
  **落在仓库外，按任务命名。** 两条都是判据，不是习惯：
  - **仓库外**——过程文件进不了 git，谁 `git add -A` 都收不到它。
    从前它写在 `--cd` 那个仓里、靠 `tmp_codex_` 前缀加一句 brief 叮嘱来防，
    2026-09-13 破了：子代理把 380 行的 `tmp_codex_final_ban_spec_review.json`
    提交进了分支，**而它的收尾自述里写着「未提交 `tmp_codex_*`」**。软约定拦不住。
    （出过事的仓另有一道 `.gitignore` 兜底，那是补历史，不是这条的替代。）
  - **按任务命名**——同时跑的几个 codex 不互相覆盖。
- **要给它新信息，只能「结束这一轮 + `resume`」**——`codex exec` 没有收件箱。
  能不能 resume 取决于**怎么结束的**，见下面「续跑」。
- **exit 1 ≠ 失败**。中途已恢复的工具 ERROR（如 apply_patch 被拒后重打成功）也会把退出码染成 1。
  判据只看产物：`jq -e '.commit // .branch' "$CODEX_HOME/reports/<任务名>.json"` 解析得开
  **且** `tail -50 codex.log` 无 `ERROR/WARN codex` 运行时行
  （`failed to refresh available models` 是良性噪声，不算）→ 成功，无视 exit 1。
  产物缺失/截断，或末尾有 ERROR → 真失败。

## 卡死 / 出问题

- **stdin 挂死**：`codex.log` 只剩 `Reading additional input from stdin` + 进程 0% CPU。
  → 锁 PID（`ps -eo pid,args | grep 'codex exec --cd <该 dir>'` 看清楚再逐个 kill），补 `</dev/null` 重起。
  **绝不 `pkill codex` / `pkill -f`**——共享机器会杀到别人的，而且 `-f` 的模式会匹配到发命令的 shell 自己。
- **codex 不靠谱** → 换 claude 子代理。

## 续跑（多轮交互）

```bash
D=$HOME/.codex-subagent; mkdir -p "$D/reports"
codex exec --cd /abs/dir resume <SESSION_ID> -o "$D/reports/<任务名>.json" "prompt"
```

**`mkdir -p` 这一句两处都不能省。** 目录不存在时 codex **不会自己建**，
`-o` 静默写失败（只在 log 最后留一行 `Failed to write last message file`），
而收尾判据是「产物没出现＝没正常收尾」——于是一次成功的运行会被判成失败。
2026-09-13 连着踩了两次。

上下文从 rollout 恢复，跨进程、跨天有效（`CODEX_HOME` 须一致）。判据同主线。
session_id 在 log 顶部，或用 `--last` 取最近一次。

### 它正在跑，而我有话要说

`codex exec` **没有收件箱**——没有任何办法往正在跑的那一轮里塞消息。给它新信息
只有一条路：**结束这一轮，然后 `resume`**。要不要为此打断它，看信息本身。

### 能不能 resume，取决于怎么结束的

| 结束方式 | 还能 resume 吗 |
|---|---|
| 正常跑完 | ✅ |
| **`kill -INT`**（SIGINT） | ✅ 上下文、token 计数都在（2026-09-08 实测） |
| `kill`（默认 SIGTERM） | ❌ **thread 永久锁死** |
| 前台跑被 2 分钟超时杀掉 | ❌ 同上——所以主线必须 `run_in_background: true` |

SIGTERM 之后再 resume 永远报
`thread-store conflict: ... already has an active writer`，**等多久都不释放**，
只能开新会话、上下文全丢。

**所以这套流程里永远不写裸 `kill`**，只写 `kill -INT`。

**同一句报错还有另一个来源，而它是良性的：上一轮根本没结束。**
一个线程只许一个写者，所以对**还在跑**的会话 resume，报的错和 SIGTERM 锁死一模一样。
2026-09-17 两次 resume 都栽在这里——判存活时用了包装链的 `$!`、以为它结束了。
**resume 之前先用真实 PID 确认真的退出了**（见「监控与收尾」第一条）：
这两种情况的处置完全相反，一个是等它跑完，一个是弃了重开。

- **`--cd` 必须放在 `resume` 之前**（放后面 clap 直接拒收）。
- resume 总用 `--cd`/当前目录覆盖 workdir，**不还原会话原目录** → 跨目录必显式带 `--cd`。
- **信号要发给 codex 本身，不是 bash 外壳**：`pgrep -f 'codex exec'` 两个都匹配，
  取错了信号打在外壳上、codex 照跑。用
  `pgrep -af 'codex exec --cd <dir>' | grep -v '/bin/bash' | awk '{print $1}'` 取 PID。
- **`-o` 的相对路径按「发命令那个 shell 的 cwd」解析，不按 `--cd`。** 实测踩过：
  resume 时没先 `cd` 过去，`-o tmp_codex_final_x.json` 落在了工作区根目录、
  在 `--cd` 那个 worktree 里怎么找都没有，一度误判成「没正常收尾」。
  **照主线写法给 `$D/reports/<任务名>.json` 这个绝对路径，这个坑就不存在了**——
  它和 cwd、和 `--cd` 都无关。
- **`resume` 收的 flag 比主线少**：`-m` / `-o` / `-c` / `--skip-git-repo-check` / `--disable`
  照常，但**`--sandbox` 它不认**（`error: unexpected argument '--sandbox' found`，退出码 2）。
  沙箱走 `-c sandbox_mode="danger-full-access"`。拿不准就先
  `codex exec resume --help`——主线那串参数直接抄过来会当场失败。
