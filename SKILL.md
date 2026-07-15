---
name: codex-sub-agent
description: 把有明确规划的执行类任务（照 DoD 写代码、对抗审查、e2e 验证）交给 codex exec 后台跑，省 claude token。用 Bash run_in_background 启动以便监控。
---

# Codex Sub-Agent（`codex exec`）

把**有明确规划的执行类任务**派给 codex 干，claude 只编排。Prompt 写进 `brief.md`（避引号地狱）。

## 启动：一个任务 = 一次 `Bash(run_in_background:true)`

**每个 codex 单独用一次 Bash 工具、`run_in_background:true` 启动**——harness 追踪它、面板可监控、完成自动通知。
**绝不 `nohup … &` 批量**：那样进程 detach，harness 追踪不到，只能手动轮询、失去监控（要起多个就多调几次 Bash）。

```bash
CODEX_HOME=$HOME/.codex-subagent codex exec --cd /abs/dir -m gpt-5.5 -c model_reasoning_effort="xhigh" \
  --sandbox danger-full-access -c approval_policy="never" -c project_doc_max_bytes=0 \
  --skip-git-repo-check --disable plugins -o final.json "$(cat brief.md)" > codex.log 2>&1 </dev/null
```

命令末尾**不加 `&`**（run_in_background 已在后台；加 `&` 反而孤立进程）。必守：

- `--cd` **绝对路径**（相对路径启动即崩：log 无 banner + `os error 2`）。
- `</dev/null` **必带**（否则 codex 等 stdin 永久挂死）。
- `-m gpt-5.5` + `model_reasoning_effort=xhigh` **定死，禁 5.6 系**（过度思考、烧配额、滥开子代理）。
- `project_doc_max_bytes=0`（不加载全局 `~/.codex/AGENTS.md`）。
- 不加 timeout（会误杀正当的长任务）。

## Skill/Plugin 隔离（防自激活）

`CODEX_HOME=$HOME/.codex-subagent` 指向专用最小配置目录：skills/、plugins/ 全空，无 MCP、无 hooks，`auth.json` 软链主配置（登录态共享），config.toml 仅钉模型。用户安装的 skill/plugin 对 codex **结构性不可见**（仅剩 5 个官方内置小工具，0.144.1 关不掉）；**brief 里固定写一句"不得使用任何 skill，除非本 brief 明确指定"** 兜底。

- **要传 skill 给它**：brief 里写明该 skill 的绝对路径（如 `~/.claude/skills/xxx/SKILL.md`），让 codex 自己读取并遵循——不动共享目录，并行任务互不污染。
- `~/.agents/skills/` 是 CODEX_HOME 管不到的共享扫描根（已清空）：别往里放东西，放了 codex 就看得见。
- 目录丢失重建：`mkdir -p ~/.codex-subagent/{skills,plugins} && ln -sfn ~/.codex/auth.json ~/.codex-subagent/auth.json` + 五行 config.toml（model/effort/approval/sandbox/service_tier）。

## 监控与收尾

- 完成自动通知（run_in_background）；中途 `tail codex.log` 查活。
- 收尾自述在 `final.json`（未出现＝未正常收尾）。
- 运行中不能中途塞消息（一次性）。
- **exit 1 ≠ 失败**（0.144.1 实证）：中途已恢复的工具 ERROR（如 apply_patch 被拒后重打成功）也会把退出码染成 1。判据只看产物：`jq -e '.commit // .branch' final.json` 可解析 **且** `tail -50 codex.log` 无 `ERROR/WARN codex` 运行时行 → 成功，无视 exit 1；产物缺失/截断或末尾有 ERROR → 真失败。

## 卡死 / 出问题

- **stdin 挂死**：`codex.log` 只剩 `Reading additional input from stdin` + 进程 0%CPU。→ 锁 PID `pgrep -af 'codex exec --cd <该 dir>'` 后 kill，补 `</dev/null` 重起。**绝不 `pkill codex`**（共享机会杀到别人的）。
- **codex 不靠谱** → 换 sonnet claude 子代理。

## 续跑

`codex exec resume <SESSION_ID> [PROMPT]`（session_id 在 log 顶部）。坑：resume **不吃 `--cd`**，workdir＝当前 cwd → 跨目录会跑错；跨目录宁可起 fresh `--cd`。
