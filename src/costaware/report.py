"""Human-readable rendering of classification results."""

from __future__ import annotations

from .classifier import Classification, Result

COST_LABELS = {
    "free": "free",
    "request": "per-request fee",
    "usage": "pay per use",
    "recurring": "recurring cost",
    "one_time": "one-time fee",
    "variable": "size-dependent, no estimate",
    "unknown": "no rule",
}


def describe(item: Classification) -> str:
    """One-line summary such as "ec2 create-nat-gateway · recurring cost · ~$32.85/month"."""
    detail = f"{item.service or '?'} {item.operation or '?'} · {COST_LABELS.get(item.cost, item.cost)}"
    if item.monthly_usd is not None:
        detail += f" · ~${item.monthly_usd:g}/month"
    if item.usd is not None:
        detail += f" · ~${item.usd:g}"
    if item.region:
        detail += f" · {item.region}"
    return detail


def headline(item: Classification, locate: bool = False) -> str:
    """describe() for a resolved call, with its place in the script when `locate` is set. The call's own text is
    only shown when it couldn't be resolved, since then it is all there is to go on."""
    if not item.service or item.service == "?" or not item.operation:
        return f"{item.command}: {describe(item)}"
    return f"{describe(item)} ({item.location})" if locate and item.location else describe(item)


def format_text(result: Result) -> str:
    if not result.items:
        return "FREE  (no AWS call found)"
    lines = [f"Result: {result.level}"]
    for item in result.items:
        lines.append(f"  {item.level:<8}{item.command}")
        lines.append(f"          {describe(item)}")
        if item.details:
            lines.append(f"          {' · '.join(item.details)}")
        if item.price:
            lines.append(f"          {item.price}")
        if item.note:
            lines.append(f"          {item.note}")
        if item.confidence != "high":
            lines.append("          (low confidence: rule needs verification)")
    return "\n".join(lines)
