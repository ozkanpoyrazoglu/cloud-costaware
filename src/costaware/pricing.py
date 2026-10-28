"""On-demand list prices from the AWS Price List API. The rule table holds no prices, only how to find them.

A rule's "price" is either the name of an estimator that builds its queries from the call's arguments
(ec2 | ebs | rds), or a list of components, each a Price List query with the unit it is quoted in:

    {"service": "AmazonEC2", "filters": {"productFamily": "NAT Gateway", "usagetype": "~NatGateway-Hours"},
     "per": "hour", "idle": "hour"}

- filters: attribute values to match; a value starting with "~" matches as a substring. The priced region is
  added as regionCode (or the attribute "region_field" names), unless the component is "global".
- exclude: {attribute: [substrings]} drops matching products the API can't filter out (FIFO queues).
- per: the unit shown after the price ("hour", "GB processed", "1M requests"); "scale" multiplies the unit
  price to match it (1000000 for "1M requests").
- idle: "hour" or "month" when the component bills while the resource exists, used or not. Those add up to
  the idle monthly cost that decides HIGH or LOW. "count" names an argument whose number of values multiplies
  it (one interface endpoint per subnet).

`price_inputs` reads what is needed from the call (no network). `annotate` runs the queries with the local AWS
CLI (`aws pricing get-products`, a free API) and sets `price` (the line shown) and `monthly_usd` (the idle
monthly cost, only when every part of it was found). Anything that goes wrong (no CLI, no credentials, a
timeout, an ambiguous match) leaves the price out.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .parser import AwsCall, argument_fields

HOURS_PER_MONTH = 730
QUERY_TIMEOUT = 6  # seconds per Price List query (AWS CLI cold starts take ~4); queries run at once, the hook has 20
CACHE_SECONDS = 24 * 3600
PRICING_REGION = "us-east-1"  # the Price List API endpoint, whatever region is priced
DEFAULT_REGION = "us-east-1"

# `--engine` values whose instance price doesn't depend on a license or edition.
RDS_ENGINES = {"postgres": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB"}


def _first(call: AwsCall, option: str) -> str | None:
    values = call.options.get(option)
    return values[0] if values else None


def _count(call: AwsCall) -> int:
    """Instances launched: `--count N` or `--count MIN:MAX` (CLI), MaxCount (API); the maximum is what can bill."""
    text = _first(call, "--count") or _first(call, "--max-count") or "1"
    try:
        return max(1, int(text.rsplit(":", 1)[-1]))
    except ValueError:
        return 1


def _volumes(call: AwsCall) -> list[tuple[int, str]]:
    """(GB, type) of each EBS volume in --block-device-mappings that states both."""
    volumes = []
    for value in call.options.get("--block-device-mappings", []):
        sizes, types = argument_fields(value, "VolumeSize"), argument_fields(value, "VolumeType")
        if len(sizes) == len(types):
            volumes += [(int(s), t) for s, t in zip(sizes, types) if s.isdigit()]
    return volumes


def _values(call: AwsCall, option: str) -> int:
    """How many values an argument has: `--subnet-ids a b`, `--subnet-ids a,b` or a JSON list."""
    count = 0
    for value in call.options.get(option, []):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        count += len(parsed) if isinstance(parsed, list) else len([v for v in value.split(",") if v.strip()])
    return count


def price_inputs(spec, call: AwsCall) -> dict:
    """What a rule's price spec needs from the call; empty when the call doesn't say enough to price it."""
    if isinstance(spec, list):
        components = []
        for component in spec:
            count = max(1, _values(call, component["count"])) if component.get("count") else 1
            components.append({**component, "quantity": count})
        return {"kind": "components", "components": components}
    if spec == "ec2" and call.operation == "run-instances" and _first(call, "--instance-type"):
        return {"kind": "ec2", "instance_type": _first(call, "--instance-type"), "count": _count(call),
                "volumes": _volumes(call)}
    if spec == "ebs" and call.operation == "create-volume":
        size, volume_type = _first(call, "--size"), _first(call, "--volume-type")
        if size and size.isdigit() and volume_type:
            return {"kind": "ebs", "volumes": [(int(size), volume_type)]}
    if spec == "rds" and call.operation == "create-db-instance":
        engine = RDS_ENGINES.get((_first(call, "--engine") or "").lower())
        instance_class = _first(call, "--db-instance-class")
        if engine and instance_class:
            return {"kind": "rds", "instance_class": instance_class, "engine": engine,
                    "multi_az": "--multi-az" in call.options}
    return {}


def _queries(inputs: dict, region: str) -> dict[str, tuple]:
    """The Price List queries an estimate needs, by name: (service code, attribute filters[, exclusions])."""
    queries = {}
    if inputs["kind"] == "components":
        for index, component in enumerate(inputs["components"]):
            filters = dict(component["filters"])
            if not component.get("global"):
                filters[component.get("region_field", "regionCode")] = region
            exclude = sorted((field, tuple(values)) for field, values in component.get("exclude", {}).items())
            queries[f"component:{index}"] = (component["service"], filters, tuple(exclude))
    if inputs["kind"] == "ec2":
        queries["instance"] = ("AmazonEC2", {
            "instanceType": inputs["instance_type"], "regionCode": region, "operatingSystem": "Linux",
            "tenancy": "Shared", "preInstalledSw": "NA", "capacitystatus": "Used",
            "licenseModel": "No License required"})
    if inputs["kind"] == "rds":
        queries["instance"] = ("AmazonRDS", {
            "instanceType": inputs["instance_class"], "regionCode": region, "databaseEngine": inputs["engine"],
            "deploymentOption": "Multi-AZ" if inputs["multi_az"] else "Single-AZ"})
    for _, volume_type in inputs.get("volumes", []):
        queries[f"ebs:{volume_type}"] = ("AmazonEC2", {
            "regionCode": region, "productFamily": "Storage", "volumeApiName": volume_type})
    return queries


def _unit_price(price_list: list, exclude=()) -> float | None:
    """The on-demand USD price when the matching products agree on one. A tiered product (first 25 hosted
    zones, first 10TB out) counts at its highest tier rate. Unused-reservation products (usage type
    "...UnusedBox...") come back alongside RDS instances and are skipped, as are products whose attribute
    contains one of the `exclude` substrings."""
    prices = set()
    for raw in price_list:
        product = json.loads(raw) if isinstance(raw, str) else raw
        attributes = product.get("product", {}).get("attributes", {})
        if "Unused" in attributes.get("usagetype", "") or any(
                value in attributes.get(field, "") for field, values in exclude for value in values):
            continue
        tiers = [float(dimension["pricePerUnit"]["USD"])
                 for term in product.get("terms", {}).get("OnDemand", {}).values()
                 for dimension in term.get("priceDimensions", {}).values()
                 if "USD" in dimension.get("pricePerUnit", {})]  # China regions are priced in CNY
        if tiers:
            prices.add(max(tiers))
    return prices.pop() if len(prices) == 1 else None


def _cache_dir(env: dict) -> Path:
    if env.get("COSTAWARE_CACHE_DIR"):
        return Path(env["COSTAWARE_CACHE_DIR"])
    return Path(env.get("HOME") or Path.home()) / ".cache" / "costaware" / "prices"


def _filter(field: str, value: str) -> dict:
    if value.startswith("~"):
        return {"Type": "CONTAINS", "Field": field, "Value": value[1:]}
    return {"Type": "TERM_MATCH", "Field": field, "Value": value}


def _lookup(service_code: str, filters: dict, aws: str, env: dict, exclude=()) -> float | None:
    """One Price List query, cached for a day. Raises on failures that mustn't be cached (timeouts, auth).
    More matches than one page holds count as ambiguous."""
    key = json.dumps([service_code, sorted(filters.items())] + ([list(exclude)] if exclude else []))
    path = _cache_dir(env) / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - cached["time"] < CACHE_SECONDS:
            return cached["usd"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    command = [aws, "pricing", "get-products", "--region", PRICING_REGION, "--service-code", service_code,
               "--max-items", "50", "--output", "json", "--no-cli-pager",
               "--filters", json.dumps([_filter(field, value) for field, value in filters.items()])]
    done = subprocess.run(command, capture_output=True, text=True, timeout=QUERY_TIMEOUT, env=env,
                          stdin=subprocess.DEVNULL, check=True)
    response = json.loads(done.stdout)
    usd = None if response.get("NextToken") else _unit_price(response.get("PriceList", []), exclude)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"time": time.time(), "usd": usd}), encoding="utf-8")
    except OSError:
        pass
    return usd


def _usd(amount: float) -> str:
    if amount >= 100:
        return f"${amount:,.0f}"
    return f"${amount:,.2f}" if amount >= 1 else f"${amount:.3g}"


def _describe_components(inputs: dict, prices: dict[str, float | None]) -> tuple[list[str], float | None]:
    """"$0.052/hour → ~$38/month", "$0.052/GB processed"... and the idle monthly total (None if a part of it
    is missing, or nothing bills while idle)."""
    parts, monthly, complete = [], 0.0, True
    for index, component in enumerate(inputs["components"]):
        price, quantity, idle = prices.get(f"component:{index}"), component["quantity"], component.get("idle")
        if price is None:
            complete = complete and not idle
            continue
        times = f" × {quantity}" if quantity > 1 else ""
        text = f"{_usd(price * component.get('scale', 1))}/{component['per']}{times}"
        if idle == "hour":
            text += f" → ~{_usd(price * quantity * HOURS_PER_MONTH)}/month"
        if idle:
            monthly += price * quantity * (HOURS_PER_MONTH if idle == "hour" else 1)
        parts.append(text)
    has_idle = any(component.get("idle") for component in inputs["components"])
    return parts, (monthly if complete and has_idle else None)


def _describe(inputs: dict, prices: dict[str, float | None]) -> tuple[list[str], str, float | None]:
    """The price parts, what they're based on, and the idle monthly cost when it is complete."""
    if inputs["kind"] == "components":
        parts, monthly = _describe_components(inputs, prices)
        return parts, "on-demand", monthly
    count = inputs.get("count", 1)
    parts = []
    if inputs["kind"] in ("ec2", "rds"):
        hourly = prices.get("instance")
        if hourly is None:
            return [], "", None
        times = f" × {count}" if count > 1 else ""
        parts.append(f"{_usd(hourly)}/hour{times} → ~{_usd(hourly * count * HOURS_PER_MONTH)}/month")
    volumes = inputs.get("volumes", [])
    storage = None
    if volumes:
        rates = [prices.get(f"ebs:{volume_type}") for _, volume_type in volumes]
        if None not in rates:
            storage = sum(size * rate for (size, _), rate in zip(volumes, rates)) * count
            sizes = ", ".join(f"{size} GB {volume_type}" for size, volume_type in volumes)
            iops = " + provisioned IOPS" if any(t in ("io1", "io2") for _, t in volumes) else ""
            parts.append(f"EBS {sizes}{' each' if count > 1 else ''} ~{_usd(storage)}/month{iops}")
    basis = {"ec2": "on-demand Linux", "rds": "on-demand, instance only", "ebs": "on-demand"}[inputs["kind"]]
    # An instance's root volume (from the AMI) and an RDS instance's storage aren't priced, so only a volume
    # on its own has a complete monthly figure.
    monthly = storage if inputs["kind"] == "ebs" and not any(t in ("io1", "io2") for _, t in volumes) else None
    return parts, basis, monthly


def annotate(items, env: dict) -> None:
    """Set `price` and `monthly_usd` on the items that carry price inputs. Needs `aws` on the PATH in `env`."""
    todo = [item for item in items if item.pricing]
    aws = shutil.which("aws", path=env.get("PATH", "")) if todo else None
    if not aws:
        return
    regions = {}
    for item in todo:
        given = item.region or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
        regions[id(item)] = (given or DEFAULT_REGION, bool(given))
    queries = {}
    for item in todo:
        for query in _queries(item.pricing, regions[id(item)][0]).values():
            queries[json.dumps(query, sort_keys=True)] = query

    def run(query):
        service_code, filters, *exclude = query
        try:
            return _lookup(service_code, filters, aws, env, *exclude)
        except Exception:  # no credentials, no network, a timeout: just no price
            return None

    with ThreadPoolExecutor(max_workers=max(1, len(queries))) as pool:
        results = dict(zip(queries, pool.map(run, queries.values())))
    for item in todo:
        region, given = regions[id(item)]
        prices = {name: results[json.dumps(query, sort_keys=True)]
                  for name, query in _queries(item.pricing, region).items()}
        parts, basis, monthly = _describe(item.pricing, prices)
        if parts:
            where = region if given else f"{region}, region not given"
            item.price = f"Price List ({basis}, {where}): {' + '.join(parts)}"
        item.monthly_usd = monthly
