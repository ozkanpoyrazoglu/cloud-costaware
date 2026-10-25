"""Classify AWS calls into FREE | LOW | HIGH | UNKNOWN using the rule table."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from importlib import resources
from pathlib import Path

from .parser import (S3_HIGH_LEVEL, AwsCall, ParseError, find_aws_calls, find_python_sources,
                     split_python_heredocs, to_cli_name)
from .pyscan import find_boto3_calls

# Severity order used when several calls are combined into one decision.
LEVELS = ("FREE", "LOW", "UNKNOWN", "HIGH")

DEFAULT_CONFIG = {
    # Recurring cost at or above this (USD/month, while idle) is HIGH, below is LOW.
    "high_monthly_usd": 1.0,
    # One-time cost at or above this (USD) is HIGH, below is LOW.
    "high_one_time_usd": 1.0,
    # Level for read operations (describe-/list-/get-...) of services missing from the rule table.
    "unknown_service_read": "UNKNOWN",
}

# Scripts larger than this are not read from disk.
MAX_SCRIPT_BYTES = 1_000_000

READ_PREFIXES = ("describe-", "list-", "get-", "head-", "batch-get-", "search-", "lookup-", "check-", "validate-")
READ_OPERATIONS = {"wait", "ls"}

# Commands that only touch local CLI state.
LOCAL_SERVICES = {"configure", "history", "help"}


@dataclass
class Classification:
    level: str
    cost: str
    service: str | None
    operation: str | None
    note: str = ""
    monthly_usd: float | None = None
    usd: float | None = None
    region: str | None = None
    rule: str = ""
    confidence: str = "high"
    command: str = ""


@dataclass
class Result:
    level: str
    items: list[Classification] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"level": self.level, "items": [asdict(item) for item in self.items]}


def load_rules(path: str | Path | None = None) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.loads(resources.files("costaware").joinpath("rules/aws.json").read_text(encoding="utf-8"))


def is_read(operation: str) -> bool:
    return operation in READ_OPERATIONS or operation.startswith(READ_PREFIXES)


def max_level(levels) -> str:
    return max(levels, key=LEVELS.index, default="FREE")


def _matches(rule: dict, call: AwsCall) -> bool:
    patterns = rule["op"] if isinstance(rule["op"], list) else [rule["op"]]
    if not any(fnmatchcase(call.operation, p) for p in patterns):
        return False
    facts = call.facts
    for key, expected in rule.get("when", {}).items():
        if key.startswith("@"):
            actual = facts.get(key[1:])
            if expected is True or expected is False:
                if (actual is not None) != expected:
                    return False
            elif actual is None or actual.lower() not in _as_lower_list(expected):
                return False
            continue
        present = key in call.options
        if expected is True or expected is False:
            if present != expected:
                return False
        elif not present or not any(v.lower() in _as_lower_list(expected) for v in call.options[key]):
            return False
    return True


def _as_lower_list(value) -> list[str]:
    return [v.lower() for v in (value if isinstance(value, list) else [value])]


def _level_for(cost: str, rule: dict, config: dict) -> str:
    if cost == "free":
        return "FREE"
    if cost in ("request", "usage"):
        return "LOW"
    if cost == "recurring":
        monthly = rule.get("monthly_usd")
        return "HIGH" if monthly is None or monthly >= config["high_monthly_usd"] else "LOW"
    if cost == "one_time":
        usd = rule.get("usd")
        return "HIGH" if usd is None or usd >= config["high_one_time_usd"] else "LOW"
    return "UNKNOWN"  # "variable" and anything unrecognised


def _from_rule(call: AwsCall, rule: dict, rule_name: str, config: dict) -> Classification:
    cost = rule.get("cost", "variable")
    return Classification(
        level=_level_for(cost, rule, config),
        cost=cost,
        service=call.service,
        operation=call.operation,
        note=rule.get("note", ""),
        monthly_usd=rule.get("monthly_usd"),
        usd=rule.get("usd"),
        region=call.region,
        rule=rule_name,
        confidence=rule.get("confidence", "high"),
        command=call.raw,
    )


def classify_call(call: AwsCall, rules: dict | None = None, config: dict | None = None) -> Classification:
    rules = rules if rules is not None else load_rules()
    config = {**DEFAULT_CONFIG, **(config or {})}

    if call.service is None or call.service in LOCAL_SERVICES or call.operation in (None, "help") \
            or call.positionals[-1:] == ["help"] or "--generate-cli-skeleton" in call.options:
        return _from_rule(call, {"cost": "free", "note": "Local CLI operation, no request is sent to AWS."}, "local", config)

    service = rules["services"].get(call.service)
    if service:
        for rule in service.get("rules", []):
            if _matches(rule, call):
                return _from_rule(call, rule, f"{call.service}:{rule['op']}", config)

    for rule in rules.get("global_rules", []):
        if _matches(rule, call):
            return _from_rule(call, rule, f"global:{rule['op']}", config)

    if service:
        key = "default_read" if is_read(call.operation) else "default_write"
        if key in service:
            return _from_rule(call, service[key], f"{call.service}:{key}", config)

    note = "This operation is not in the rule table." if service else f"Service '{call.service}' is not in the rule table."
    item = _from_rule(call, {"cost": "unknown", "note": note}, "fallback", config)
    if not service and is_read(call.operation):
        item.level = config["unknown_service_read"]
    return item


def classify_command(command: str, rules: dict | None = None, config: dict | None = None,
                     cwd: str | None = None) -> Result:
    """Classify every `aws` invocation, and every boto3 call in Python run by it, in a shell command.

    `cwd` is where relative script paths (`python scripts/x.py`) are resolved; defaults to the current directory.
    """
    rules = rules if rules is not None else load_rules()
    items = []
    shell, _ = split_python_heredocs(command)
    try:
        items += [classify_call(call, rules, config) for call in find_aws_calls(shell)]
    except ParseError as exc:
        if "aws" in shell:
            items.append(Classification("UNKNOWN", "unknown", None, None, rule="parse-error", command=command,
                                        note=f"Could not parse the command: {exc}"))
    for source in find_python_sources(command):
        if source.kind == "inline":
            items += classify_python(source.value, rules, config).items
            continue
        path = os.path.join(cwd or os.getcwd(), os.path.expanduser(source.value))
        try:
            if os.path.getsize(path) <= MAX_SCRIPT_BYTES:
                with open(path, encoding="utf-8") as f:
                    items += classify_python(f.read(), rules, config, label=source.value).items
        except (OSError, UnicodeDecodeError):
            pass  # a script we can't read is not ours to judge; the shell rules still apply
    return Result(max_level(i.level for i in items), items)


def _worst_conditional_level(rules: dict, config: dict | None, service: str | None, operation: str | None) -> str:
    """Highest level an argument-dependent rule for this operation can produce ("FREE" if there is none)."""
    config = {**DEFAULT_CONFIG, **(config or {})}
    call = AwsCall(service, operation)
    levels = [_level_for(rule.get("cost", "variable"), rule, config)
              for rule in rules["services"].get(service or "", {}).get("rules", [])
              if rule.get("when") and _matches({**rule, "when": {}}, call)]
    return max_level(levels)


def classify_python(code: str, rules: dict | None = None, config: dict | None = None,
                    aws_context: bool = False, label: str = "") -> Result:
    """Classify the AWS calls in Python source (a script, `python -c`, or an AWS MCP script tool).

    `aws_context` marks code that is known to run against AWS, so unresolvable calls count as UNKNOWN.
    """
    rules = rules if rules is not None else load_rules()
    prefix = f"{label}: " if label else ""
    try:
        calls = find_boto3_calls(code, aws_context)
    except ParseError as exc:
        if not (aws_context or "boto3" in code or "call_boto3" in code):
            return Result("FREE")
        item = Classification("UNKNOWN", "unknown", None, None, note=f"Could not parse the script: {exc}",
                              rule="parse-error", command=prefix + "<script>")
        return Result("UNKNOWN", [item])
    items = []
    for call in calls:
        command = f"{prefix}{call.source} (line {call.line})"
        if call.note or call.service in (None, "?") or call.operation is None:
            items.append(Classification("UNKNOWN", "unknown", call.service, call.operation, region=call.region,
                                        note=call.note or "The client's service is computed at runtime.",
                                        rule="python-unresolved", command=command))
            continue
        operation = to_cli_name(call.operation).replace("_", "-")  # CreateNatGateway or create_nat_gateway
        item = classify_operation(call.service, operation, call.params, rules, config).items[0]
        item.command, item.region = command, call.region
        worst = _worst_conditional_level(rules, config, item.service, item.operation) if call.dynamic_params else "FREE"
        if LEVELS.index(worst) > LEVELS.index(item.level):
            item.level, item.confidence = worst, "low"
            item.note = (item.note + " The cost depends on arguments only known at runtime;"
                         " assuming the costliest case.").strip()
        items.append(item)
    return Result(max_level(i.level for i in items), items)


def classify_operation(service: str, operation: str, params: dict | None = None,
                       rules: dict | None = None, config: dict | None = None) -> Result:
    """Classify an API-style call (e.g. from an MCP tool: service="ec2", operation="RunInstances")."""
    operation = to_cli_name(operation)
    if service == "s3" and operation not in S3_HIGH_LEVEL:
        service = "s3api"
    options = {}
    for key, value in (params or {}).items():
        name = "--" + to_cli_name(key)
        if value is True:
            options[name] = []
        elif value is not False and value is not None:
            options[name] = [str(v) for v in value] if isinstance(value, list) else [str(value)]
    call = AwsCall(service, operation, options, raw=f"{service} {operation}")
    item = classify_call(call, rules, config)
    return Result(item.level, [item])
