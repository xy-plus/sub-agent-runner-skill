#!/usr/bin/env python3
"""Bash 护栏：传递调用事实，具体是否派活由 runner 运行时决定。"""
import argparse
import json
import shlex
import sys

import sub_agent_runner as runner


def mark_call(payload):
    tool_input = payload["tool_input"]
    command = tool_input["command"]
    # 这里只限制改写范围，不解析 shell 的执行语义。grep/printf 即便提到名字，
    # 没有实际运行 runner 就不会被拒绝；无名字的间接派活是用户接受的例外。
    if "sub-agent-runner" not in command and "sub_agent_runner" not in command:
        return 0
    mode = "background" if tool_input.get("run_in_background") is True else "foreground"
    marker = shlex.quote(f"{mode}:{payload['tool_use_id']}")
    trap = ("trap 'rc=$?; G=\"$HOME/.claude/skills/sub-agent-runner/background_guard.py\"; "
            'if [ -x "$G" ]; then "$G" end-call "$rc"; exit $?; fi; exit $rc\' EXIT')
    updated = dict(tool_input, command=f"export {runner.BASH_CALL_ENV}={marker}\n{trap}\n{command}")
    # 不返回 permissionDecision：权限流程仍判断改写后的原调用。
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                            "updatedInput": updated}}, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("mark-call",))
    parser.parse_args()
    return mark_call(json.load(sys.stdin))


if __name__ == "__main__":
    sys.exit(main())
