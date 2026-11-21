#!/usr/bin/env python3
"""Antigravity's PreToolUse hook in a build container. It lets the model
read and write files and run commands, and refuses every other tool: web
search, the browser, URL reading, subagents, and the rest. The container's
network already reaches only the model provider, but a search the provider
runs on its own servers would get around that.

Adapted from tsumegoBench's agy-tool-policy.py.
"""

import json
import sys

ALLOWED = {
    # Reading.
    "read_file", "view_file", "sed_file", "list_dir", "find_by_name", "grep_search",
    # Writing.
    "write_file", "write_to_file", "edit_file", "replace_file_content", "multi_replace_file_content",
    # Commands.
    "run_command", "command_status", "send_command_input", "wait", "wait_5_seconds", "finish",
}


def decision(payload):
    call = payload.get("toolCall") or {}
    name, args = call.get("name"), call.get("args", {})
    if not isinstance(args, dict):
        return {"decision": "deny", "reason": "Invalid tool arguments."}
    if name not in ALLOWED:
        return {"decision": "deny", "reason": "Only local file and command tools are available here."}
    return {"decision": "allow"}


if __name__ == "__main__":
    try:
        result = decision(json.load(sys.stdin))
    except (OSError, ValueError, TypeError, AttributeError):
        result = {"decision": "deny", "reason": "Invalid tool request."}
    print(json.dumps(result))
