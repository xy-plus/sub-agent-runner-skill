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
codex exec --cd /abs/dir -m gpt-5.5 -c model_reasoning_effort="xhigh" \
  --sandbox danger-full-access -c approval_policy="never" -c project_doc_max_bytes=0 \
  --skip-git-repo-check -o final.json "$(cat brief.md)" > codex.log 2>&1 </dev/null
```

命令末尾**不加 `&`**（run_in_background 已在后台；加 `&` 反而孤立进程）。必守：

- `--cd` **绝对路径**（相对路径启动即崩：log 无 banner + `os error 2`）。
- `</dev/null` **必带**（否则 codex 等 stdin 永久挂死）。
- `-m gpt-5.5` + `model_reasoning_effort=xhigh` **定死，禁 5.6 系**（过度思考、烧配额、滥开子代理）。
- `project_doc_max_bytes=0`（不加载全局 `~/.codex/AGENTS.md`）。
- 不加 timeout（会误杀正当的长任务）。

## 监控与收尾

- 完成自动通知（run_in_background）；中途 `tail codex.log` 查活。
- 收尾自述在 `final.json`（未出现＝未正常收尾）。
- 运行中不能中途塞消息（一次性）。

## 卡死 / 出问题

- **stdin 挂死**：`codex.log` 只剩 `Reading additional input from stdin` + 进程 0%CPU。→ 锁 PID `pgrep -af 'codex exec --cd <该 dir>'` 后 kill，补 `</dev/null` 重起。**绝不 `pkill codex`**（共享机会杀到别人的）。
- **codex 不靠谱** → 换 sonnet claude 子代理。

## 续跑

`codex exec resume <SESSION_ID> [PROMPT]`（session_id 在 log 顶部）。坑：resume **不吃 `--cd`**，workdir＝当前 cwd → 跨目录会跑错；跨目录宁可起 fresh `--cd`。
