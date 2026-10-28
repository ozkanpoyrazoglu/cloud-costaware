"""Command-line entry point: `costaware classify ...` and `costaware hook`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .classifier import DEFAULT_CONFIG, apply_prices, classify_command, classify_operation, classify_python, load_rules
from .report import format_text

def _key_value(text: str) -> tuple[str, object]:
    key, sep, value = text.partition("=")
    if not sep:
        return key, True
    try:
        return key, json.loads(value)
    except json.JSONDecodeError:
        return key, value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="costaware", description="Classify AWS calls by cost.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    classify = sub.add_parser("classify", help="Classify a shell command or an API call.")
    classify.add_argument("command", nargs="?", help="Shell command. Read from stdin if '-' or omitted.")
    classify.add_argument("--service", help="Service for an API call (e.g. ec2).")
    classify.add_argument("--operation", help="Operation for an API call (e.g. RunInstances or run-instances).")
    classify.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                          help="API parameter, repeatable (e.g. BillingMode=PAY_PER_REQUEST).")
    classify.add_argument("--python", metavar="FILE",
                          help="Classify the boto3 calls in a Python file ('-' for stdin) instead of a shell command.")
    classify.add_argument("--format", choices=["text", "json"], default="text")
    classify.add_argument("--rules", help="Alternative rule table (JSON).")
    classify.add_argument("--config", help="JSON file with threshold settings.")
    classify.add_argument("--no-price", action="store_true",
                          help="Don't look up prices (recurring costs then stay HIGH).")

    hook = sub.add_parser("hook", help="Run as a PreToolUse hook (payload on stdin).")
    hook.add_argument("--client", choices=["claude", "antigravity", "codex"],
                      help="Hook payload format. Detected from the payload when omitted.")

    args = parser.parse_args(argv)
    if args.cmd == "hook":
        from .hook import main as hook_main
        return hook_main(args.client)

    rules = load_rules(args.rules)
    config = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else None

    if args.python:
        code = sys.stdin.read() if args.python == "-" else Path(args.python).read_text(encoding="utf-8")
        result = classify_python(code, rules, config, label="" if args.python == "-" else args.python)
    elif args.service or args.operation:
        if not (args.service and args.operation):
            parser.error("--service and --operation must be given together")
        params = dict(_key_value(p) for p in args.param)
        result = classify_operation(args.service, args.operation, params, rules, config)
    else:
        command = args.command if args.command not in (None, "-") else sys.stdin.read()
        result = classify_command(command, rules, config)

    if not args.no_price and {**DEFAULT_CONFIG, **(config or {})}.get("price_lookup") is True:
        result = apply_prices(result, dict(os.environ), config)

    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
