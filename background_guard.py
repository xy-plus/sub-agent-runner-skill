#!/usr/bin/env python3
"""Bash 护栏：传递调用事实，shell 退出时等脱离任务并收集结果。

不能从 shell 文本同时判准派活与普通数据，也不能让 runner 自己判断被 &/setsid
脱离了没有。只有这次调用的 shell 即将退出时，仍带着本次标记的真实 runner
才是该等的对象。继续使用 Bash 后台完成通知，不新建通知器、祖先链或启动登记。
"""
import argparse
import json
import os
import pathlib
import shlex
import shutil
import sys
import time

import sub_agent_runner as runner

RUNNER_SCRIPT = pathlib.Path(runner.__file__).resolve()


def mark_call(payload):
    tool_input = payload["tool_input"]
    command = tool_input["command"]
    # 这里只限制改写范围，不解析 shell 的执行语义。grep/printf 即便提到名字，
    # 没有实际运行 runner 就不会被拒绝；无名字的间接派活是用户接受的例外。
    if "sub-agent-runner" not in command and "sub_agent_runner" not in command:
        return 0
    mode = "background" if tool_input.get("run_in_background") is True else "foreground"
    marker = shlex.quote(f"{mode}:{payload['tool_use_id']}")
    # 在 source && eval && pwd 包裹的外层 shell 退出时检查；不能只把检查附到正文
    # 后面，false 或 exit 会跳过它。用户接受清标记、覆盖 trap/exec、隐藏命令名及
    # 调用结束后才启动的四种绕过，不为这些情况再叠第二套护栏。
    trap = ("trap 'rc=$?; G=\"$HOME/.claude/skills/sub-agent-runner/background_guard.py\"; "
            'if [ -x "$G" ]; then "$G" end-call "$rc"; exit $?; fi; exit $rc\' EXIT')
    updated = dict(tool_input, command=f"export {runner.BASH_CALL_ENV}={marker}\n{trap}\n{command}")
    # 不返回 permissionDecision：权限流程仍判断改写后的原调用。
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                            "updatedInput": updated}}, ensure_ascii=False))
    return 0


def python_script_index(argv):
    """Python 实际执行的脚本位置；-c、-m、标准输入都没有脚本可比对。

    不在任意 argv 元素中搜索 runner 名字，那会把 grep/printf 的数据和 -c 的
    额外参数当成脚本。解释器选项可能合写，-X/-W 的值可能在本词或下一个词。
    """
    index = 1
    while index < len(argv):
        option = argv[index]
        if option == "--":
            index += 1
            return index if index < len(argv) and argv[index] != "-" else None
        if option == "-":
            return None
        if not option.startswith("-"):
            return index
        if option == "--check-hash-based-pycs":
            index += 2
            continue
        for position, flag in enumerate(option[1:], 1):
            if flag in "cm":
                return None
            if flag in "XW":
                if position == len(option) - 1:
                    index += 1
                break
        index += 1
    return None


def find_call_runners(marker):
    """/proc 只回答此刻还要等谁；已经退出者的成败由调用目录回答。

    精确核对用户、Python 执行的脚本真实路径、启动子命令和完整环境标记。
    相对脚本路径按目标进程的 cwd 解释，不能借用退出检查自己的 cwd。
    """
    entry = os.fsencode(f"{runner.BASH_CALL_ENV}={marker}")
    found = {}
    for process in pathlib.Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            if not pathlib.Path(os.readlink(process / "exe")).name.startswith("python"):
                continue
            argv = [os.fsdecode(part) for part in (process / "cmdline").read_bytes().split(b"\0")[:-1]]
            index = python_script_index(argv)
            if index is None or index + 1 >= len(argv) or argv[index + 1] not in runner._KINDS:
                continue
            script = pathlib.Path(argv[index])
            if not script.is_absolute():
                script = process / "cwd" / script
            if script.resolve() != RUNNER_SCRIPT:
                continue
            if entry not in (process / "environ").read_bytes().split(b"\0"):
                continue
            fields = runner._read_stat_fields(process.name)
            if fields is None or fields[0] in ("Z", "X"):
                continue
            arguments = argv[index + 1:]
            if arguments[0] == "run":
                task = next((arg.split("=", 1)[1] for arg in arguments if arg.startswith("--task=")), "?")
                if "--task" in arguments:
                    task = arguments[arguments.index("--task") + 1]
            else:
                task = arguments[1] if len(arguments) > 1 else "?"
            found[int(process.name)] = (task, fields[1])
        except (OSError, IndexError):
            # 进程可能在扫描任何一个文件之间退出；它先写的结论仍会被全部读取。
            continue
    return found


def end_call(original_code):
    call = runner.bash_call()
    if call is None:
        return original_code
    waited = find_call_runners(os.environ[runner.BASH_CALL_ENV])
    if waited and call[0] == "background":
        names = "、".join(sorted({task for task, _ in waited.values()}))
        print(f"[sub-agent-runner] 这次调用里的 runner 被脱离了，等它们跑完：{names}")
    alive = dict(waited)
    # setsid/嵌套 shell 的 runner 不是本进程的子进程，waitpid 够不着。复用 runner
    # 的 stat 读取，同时核启动时刻；僵尸已退出，不必等它被自己的父进程回收。
    while alive:
        for pid, (_, start) in list(alive.items()):
            fields = runner._read_stat_fields(str(pid))
            if fields is None or fields[0] in ("Z", "X") or fields[1] != start:
                del alive[pid]
        if alive:
            time.sleep(0.05)

    directory = runner.bash_call_directory(call[1])
    refused = (directory / "refused").exists()
    if refused:
        print(runner.FOREGROUND_REFUSAL)
    worst, seen = "success", set()
    states_by_code = {code: state for state, code in runner._STATES}
    # 必须等完再读全部记录，包括扫描前就已失败退出的任务。正确直接派活也会
    # 多打印一行结论；接受重复，避免另加「是否已经呈现」的状态或启动登记。
    for path in sorted(directory.glob("*.done")):
        result = json.loads(path.read_text())
        code = result["exit_code"]
        print(f"{result['verdict']}（退出码 {code}）")
        worst = runner._worse(worst, states_by_code[code])
        seen.add(int(path.stem))
    for pid, (task, _) in waited.items():
        if pid not in seen:
            print(f"[sub-agent-runner] {task}：没留下结论（退出码按 1 计）")
            worst = runner._worse(worst, "failed")
    if directory.is_dir():
        shutil.rmtree(directory)
    if original_code != 0:
        # 例如独立一句 "$(runner ...)"：内部拒绝为 2，外层空命令为 127。
        # 保留外层的 127，不能为了统一拒绝码把原命令自己的错误抹掉。
        return original_code
    return runner.USAGE_ERROR if refused else runner.EXIT[worst]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("mark-call")
    end = commands.add_parser("end-call")
    end.add_argument("original_code", type=int)
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    if args.action == "mark-call":
        return mark_call(json.load(sys.stdin))
    try:
        return end_call(args.original_code)
    except Exception as error:
        # 用户明确接受脚本坏了时放行；只报告错误，不修复、不改原调用的结果。
        print(f"[sub-agent-runner 护栏] end-call 出错：{error!r}", file=sys.stderr)
        return args.original_code


if __name__ == "__main__":
    sys.exit(main())
