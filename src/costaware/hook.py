"""PreToolUse hook for Claude Code, Antigravity CLI and Codex CLI: classify AWS calls before they run.

Reads the hook payload from stdin and prints a hook response on stdout. The client is detected from the
payload's shape. Decisions:

- FREE: the normal permission flow applies.
- LOW: the normal permission flow applies; the cost note is passed along so the agent can tell the user.
- HIGH / UNKNOWN: the user must confirm before the call runs.
  - Claude Code: "ask", the normal permission prompt (it also overrides allow rules such as Bash(aws:*)).
  - Antigravity: its "ask"/"force_ask" decisions are skipped when permissions are bypassed
    (--dangerously-skip-permissions), so the call is denied and approved in chat instead. The agent is given a
    code tied to that exact call; the call runs once the user's own message contains "costaware approve <code>".
  - Codex: hooks can't ask (an "ask" is treated as a hook failure and the call runs), and the user's message
    isn't part of the PreToolUse payload. The same chat approval is used; a UserPromptSubmit hook records the
    codes in the user's latest prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from .classifier import (DEFAULT_CONFIG, LEVELS, Classification, Result, apply_prices, classify_command,
                         classify_operation, classify_python, load_rules, max_level)
from .parser import is_plain_aws
from .report import headline

ASK_LEVELS = {"HIGH", "UNKNOWN"}

# Argument names AWS MCP servers use for CLI-style and API-style calls.
COMMAND_KEYS = ("cli_command", "command", "cmd", "aws_command")
# Python run against AWS, e.g. the AWS MCP Server's aws___run_script(code=...).
CODE_KEYS = ("code", "script", "python_code")
SERVICE_KEYS = ("service", "service_name", "serviceName")
OPERATION_KEYS = ("operation", "operation_name", "operationName", "action", "api")
PARAM_KEYS = ("parameters", "params", "arguments", "args", "input")

PROJECT_CONFIG = ".costaware.json"

APPROVAL = re.compile(r"\bcostaware\s+approve\s+([0-9a-f]{6})\b", re.IGNORECASE)


def user_config_path(env: dict) -> Path:
    if env.get("COSTAWARE_CONFIG"):
        return Path(env["COSTAWARE_CONFIG"])
    base = env.get("XDG_CONFIG_HOME") or Path(env.get("HOME") or Path.home()) / ".config"
    return Path(base) / "costaware" / "config.json"


def _stricter(base: dict, project: dict) -> dict:
    """Apply only the project settings that make the gate stricter."""
    config = dict(base)
    if isinstance(project.get("high_monthly_usd"), (int, float)):
        config["high_monthly_usd"] = min(config["high_monthly_usd"], project["high_monthly_usd"])
    level = project.get("unknown_service_read")
    if level in LEVELS and LEVELS.index(level) > LEVELS.index(config["unknown_service_read"]):
        config["unknown_service_read"] = level
    return config


def load_config(env: dict, project_dir: str | None = None) -> dict:
    """The user's config (COSTAWARE_CONFIG, or ~/.config/costaware/config.json), tightened by .costaware.json.

    The project file lives in the agent's workspace, where the agent can write, so it may only make the gate
    stricter. Loosening it takes the user-level file.
    """
    path = user_config_path(env)
    config = {**DEFAULT_CONFIG, **(json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {})}
    project_dir = project_dir or env.get("CLAUDE_PROJECT_DIR")
    project = Path(project_dir) / PROJECT_CONFIG if project_dir else None
    if project and project.is_file():
        config = _stricter(config, json.loads(project.read_text(encoding="utf-8")))
    return config


@dataclass
class ToolCall:
    """A tool call, normalized across clients."""

    name: str
    input: dict
    shell_command: str | None = None  # set when the tool is the client's shell tool
    cwd: str | None = None
    project_dir: str | None = None
    external: bool = False  # an MCP (or other non-built-in) tool whose arguments may carry AWS calls
    user_input: str | None = None  # the user's latest message, as reported by the client (never model-written)

    @property
    def approval_code(self) -> str:
        """Short code tied to this call, so approving one call doesn't approve another.

        Whitespace is normalized, so a retry that only differs in spacing keeps its approval.
        """
        if self.shell_command is not None:
            body = _normalize_shell(self.shell_command)
        else:
            body = json.dumps(_normalize_value(self.input), sort_keys=True)
        return hashlib.sha256(f"{self.name}\0{body}".encode()).hexdigest()[:6]

    def approved(self) -> bool:
        return self.approval_code in {m.lower() for m in APPROVAL.findall(self.user_input or "")}


def parse_claude(payload: dict, env: dict) -> ToolCall:
    name, tool_input = payload.get("tool_name", ""), payload.get("tool_input") or {}
    command = tool_input.get("command", "") if name == "Bash" else None
    return ToolCall(name, tool_input, command, payload.get("cwd"), None, name.startswith("mcp__"))


def _normalize_shell(command: str) -> str:
    try:
        return shlex.join(shlex.split(command))
    except ValueError:
        return " ".join(command.split())


def _normalize_value(value):
    if isinstance(value, str):
        return "\n".join(line.rstrip() for line in value.strip().splitlines())
    if isinstance(value, dict):
        return {k: _normalize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_value(v) for v in value]
    return value


def approvals_path(session_id: str, env: dict) -> Path:
    base = Path(env.get("COSTAWARE_STATE_DIR") or Path.home() / ".cache" / "costaware" / "approvals")
    return base / (re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "default") + ".txt")


def record_user_prompt(payload: dict, env: dict) -> None:
    """Keep the approvals in the user's latest prompt; a new prompt replaces the previous ones."""
    path = approvals_path(payload.get("session_id", ""), env)
    codes = sorted({c.lower() for c in APPROVAL.findall(payload.get("prompt") or "")})
    if codes:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"costaware approve {c}\n" for c in codes), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def parse_codex(payload: dict, env: dict) -> ToolCall:
    call = parse_claude(payload, env)
    path = approvals_path(payload.get("session_id", ""), env)
    call.user_input = path.read_text(encoding="utf-8") if path.is_file() else None
    return call


def parse_antigravity(payload: dict, env: dict) -> ToolCall:
    call = payload.get("toolCall") or {}
    name, args = call.get("name", ""), call.get("args") or {}
    workspaces = payload.get("workspacePaths") or []
    project_dir = workspaces[0] if workspaces else None
    user_input = payload.get("lastUserInput")
    if name == "run_command":
        return ToolCall(name, args, args.get("CommandLine", ""), args.get("Cwd") or project_dir, project_dir,
                        user_input=user_input)
    if name == "call_mcp_tool":
        # MCP calls go through one wrapper tool: {"ServerName": ..., "ToolName": ..., "Arguments": {...}}.
        arguments = args.get("Arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"code": arguments}  # unparsable: still let the Python analyzer look at it
        tool = f"{args.get('ServerName', '')}/{args.get('ToolName', '')}"
        return ToolCall(tool, arguments if isinstance(arguments, dict) else {}, None, project_dir, project_dir,
                        external=True, user_input=user_input)
    # Other tools' arguments are checked too, in case a client exposes MCP tools directly.
    return ToolCall(name, args, None, project_dir, project_dir, external=True, user_input=user_input)


def _first(mapping: dict, keys) -> object:
    return next((mapping[k] for k in keys if mapping.get(k)), None)


def classify_mcp_input(tool_name: str, tool_input: dict, rules: dict, config: dict | None) -> Result:
    """Find AWS calls in an MCP tool's arguments.

    Handles CLI-style tools (e.g. call_aws, whose `cli_command` is a string or a list), script tools
    (e.g. the AWS MCP Server's aws___run_script, whose `code` is boto3 Python), and API-style tools that pass
    a service, an operation and a parameter dict.
    """
    items: list[Classification] = []
    aws_tool = "aws" in tool_name.lower()
    for key in CODE_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            items += classify_python(value, rules, config, aws_context=aws_tool).items
    for key in COMMAND_KEYS:
        value = tool_input.get(key)
        for command in value if isinstance(value, list) else [value]:
            if isinstance(command, str) and "aws" in command:
                items += classify_command(command, rules, config).items
    service, operation = _first(tool_input, SERVICE_KEYS), _first(tool_input, OPERATION_KEYS)
    if isinstance(service, str) and isinstance(operation, str):
        params = _first(tool_input, PARAM_KEYS)
        params = params if isinstance(params, dict) else {}
        items += classify_operation(service.lower(), operation, params, rules, config).items
    return Result(max_level(i.level for i in items), items)


def touches_approvals(call: ToolCall, env: dict) -> bool:
    """Whether the call writes the approval phrase or reaches into the approval state (Codex reads approvals
    from a file, so the agent must not create them itself)."""
    text = call.shell_command if call.shell_command is not None else json.dumps(call.input)
    markers = ("costaware/approvals", str(approvals_path("", env).parent))
    return bool(APPROVAL.search(text)) or any(m in text for m in markers)


def classify_tool_call(call: ToolCall, env: dict) -> Result:
    rules = load_rules(env.get("COSTAWARE_RULES"))
    config = load_config(env, call.project_dir)
    if call.shell_command is not None:
        result = classify_command(call.shell_command, rules, config, cwd=call.cwd)
    elif call.external:
        result = classify_mcp_input(call.name, call.input, rules, config)
    else:
        result = Result("FREE")
    if config.get("price_lookup") is True:
        result = apply_prices(result, env, config)
    if touches_approvals(call, env):
        item = Classification("HIGH", "unknown", None, None, rule="approval-state", command="<tool call>",
                              note="The call touches costaware's approvals. Only the user can approve a call.")
        result = Result("HIGH", [*result.items, item])
    return result


def summarize(result: Result) -> str:
    lines = [f"costaware: {result.level}"]
    shown = [item for item in result.items if item.level != "FREE"]
    for item in shown:
        lines.append(f"- [{item.level}] {headline(item, locate=len(shown) > 1)}")
        if item.details:
            lines.append(f"  {' · '.join(item.details)}")
        if item.price:
            lines.append(f"  {item.price}")
        if item.note:
            lines.append(f"  {item.note}")
        if item.confidence != "high":
            lines.append("  (low confidence: rule needs verification)")
    return "\n".join(lines)


def render_claude(result: Result, call: ToolCall) -> dict | None:
    if result.level == "FREE":
        return None
    summary = summarize(result)
    output = {"hookEventName": "PreToolUse", "additionalContext": summary}
    if result.level in ASK_LEVELS:
        output["permissionDecision"] = "ask"
        output["permissionDecisionReason"] = summary
    return {"hookSpecificOutput": output}


def render_antigravity(result: Result, call: ToolCall) -> dict:
    # "allow" defers to the next hook and the user's permission rules; it does not skip them.
    if result.level == "FREE":
        return {"decision": "allow"}
    summary = summarize(result)
    if result.level not in ASK_LEVELS:
        return {"decision": "allow", "reason": summary}
    if call.approved():
        # The user approved this exact call in chat. Skip the client's own prompt only when the gate has seen
        # everything the call does; otherwise (pipes, other programs, scripts) the user's permission rules apply.
        reason = f"{summary}\nApproved by the user in chat."
        plain = call.shell_command is None or is_plain_aws(call.shell_command)
        return {"decision": "auto_approve" if plain else "allow", "reason": reason}
    return {"decision": "deny", "reason": denial_reason(summary, call)}


def denial_reason(summary: str, call: ToolCall) -> str:
    return (f"{summary}\n"
            "This call was stopped before it ran. Tell the user what it will cost (see the costaware skill), then"
            f" ask them to reply with exactly: costaware approve {call.approval_code}\n"
            "Retry the identical call only after the user has sent that message. Do not change the call to get"
            " around this check.")


def render_codex(result: Result, call: ToolCall) -> dict | None:
    if result.level == "FREE":
        return None
    summary = summarize(result)
    output = {"hookEventName": "PreToolUse", "additionalContext": summary}
    if result.level in ASK_LEVELS and not call.approved():
        output["permissionDecision"] = "deny"
        output["permissionDecisionReason"] = denial_reason(summary, call)
    return {"hookSpecificOutput": output}


CLIENTS = {
    "claude": (parse_claude, render_claude),
    "antigravity": (parse_antigravity, render_antigravity),
    "codex": (parse_codex, render_codex),
}


def detect_client(payload: dict) -> str:
    if "toolCall" in payload:
        return "antigravity"
    return "codex" if "turn_id" in payload else "claude"


def run(payload: dict, env: dict, client: str | None = None) -> dict | None:
    client = client or detect_client(payload)
    if payload.get("hook_event_name") == "UserPromptSubmit":
        if client == "codex":
            record_user_prompt(payload, env)
        return None
    parse, render = CLIENTS[client]
    call = ToolCall("unknown", payload)
    try:
        call = parse(payload, env)
        return render(classify_tool_call(call, env), call)
    except Exception as exc:  # a broken gate must not silently let AWS calls through
        if "aws" not in json.dumps(payload).lower():
            return render(Result("FREE"), call)
        item = Classification("UNKNOWN", "unknown", None, None, rule="error", command="<tool call>",
                              note=f"costaware failed ({type(exc).__name__}: {exc}); confirm this call manually.")
        return render(Result("UNKNOWN", [item]), call)


def main(client: str | None = None) -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    response = run(payload, dict(os.environ), client)
    if response:
        print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
