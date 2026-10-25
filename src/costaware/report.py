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


def format_text(result: Result) -> str:
    if not result.items:
        return "FREE  (no AWS call found)"
    lines = [f"Result: {result.level}"]
    for item in result.items:
        lines.append(f"  {item.level:<8}{item.command}")
        lines.append(f"          {describe(item)}")
        if item.note:
            lines.append(f"          {item.note}")
        if item.confidence != "high":
            lines.append("          (low confidence: rule needs verification)")
    return "\n".join(lines)
