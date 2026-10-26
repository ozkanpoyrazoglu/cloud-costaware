"""On-demand list prices from the AWS Price List API, shown next to the confirmation.

Rules name an estimator with "price". `price_inputs` reads what it needs from the call (no network), and
`annotate` looks the prices up with the local AWS CLI (`aws pricing get-products`, a free API) when the hook runs.
A price is only ever shown; it never changes the level. Anything that goes wrong (no CLI, no credentials, a
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


def price_inputs(kind: str | None, call: AwsCall) -> dict:
    """What estimator `kind` needs from the call; empty when the call doesn't say enough to price it."""
    if kind == "ec2" and call.operation == "run-instances" and _first(call, "--instance-type"):
        return {"kind": "ec2", "instance_type": _first(call, "--instance-type"), "count": _count(call),
                "volumes": _volumes(call)}
    if kind == "ebs" and call.operation == "create-volume":
        size, volume_type = _first(call, "--size"), _first(call, "--volume-type")
        if size and size.isdigit() and volume_type:
            return {"kind": "ebs", "volumes": [(int(size), volume_type)]}
    if kind == "rds" and call.operation == "create-db-instance":
        engine = RDS_ENGINES.get((_first(call, "--engine") or "").lower())
        instance_class = _first(call, "--db-instance-class")
        if engine and instance_class:
            return {"kind": "rds", "instance_class": instance_class, "engine": engine,
                    "multi_az": "--multi-az" in call.options}
    return {}


def _queries(inputs: dict, region: str) -> dict[str, tuple[str, dict]]:
    """The Price List queries an estimate needs, by name: (service code, attribute filters)."""
    queries = {}
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


def _unit_price(price_list: list) -> float | None:
    """The on-demand USD price when exactly one product matches. Unused-reservation products
    (usage type "...UnusedBox...") come back alongside RDS instances and are skipped."""
    prices = set()
    for raw in price_list:
        product = json.loads(raw) if isinstance(raw, str) else raw
        if "Unused" in product.get("product", {}).get("attributes", {}).get("usagetype", ""):
            continue
        for term in product.get("terms", {}).get("OnDemand", {}).values():
            for dimension in term.get("priceDimensions", {}).values():
                prices.add(float(dimension["pricePerUnit"]["USD"]))
    return prices.pop() if len(prices) == 1 else None


def _cache_dir(env: dict) -> Path:
    if env.get("COSTAWARE_CACHE_DIR"):
        return Path(env["COSTAWARE_CACHE_DIR"])
    return Path(env.get("HOME") or Path.home()) / ".cache" / "costaware" / "prices"


def _lookup(service_code: str, filters: dict, aws: str, env: dict) -> float | None:
    """One Price List query, cached for a day. Raises on failures that mustn't be cached (timeouts, auth)."""
    key = json.dumps([service_code, sorted(filters.items())])
    path = _cache_dir(env) / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - cached["time"] < CACHE_SECONDS:
            return cached["usd"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    command = [aws, "pricing", "get-products", "--region", PRICING_REGION, "--service-code", service_code,
               "--max-items", "10", "--output", "json", "--no-cli-pager", "--filters",
               *(f"Type=TERM_MATCH,Field={field},Value={value}" for field, value in filters.items())]
    done = subprocess.run(command, capture_output=True, text=True, timeout=QUERY_TIMEOUT, env=env,
                          stdin=subprocess.DEVNULL, check=True)
    usd = _unit_price(json.loads(done.stdout).get("PriceList", []))
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


def _describe(inputs: dict, prices: dict[str, float | None], region: str, region_given: bool) -> str | None:
    """"Price List (...): $0.46/hour → ~$336/month + EBS 30 GB gp3 ~$2.86/month"; None if a price is missing."""
    count = inputs.get("count", 1)
    parts, basis = [], []
    if inputs["kind"] in ("ec2", "rds"):
        hourly = prices.get("instance")
        if hourly is None:
            return None
        times = f" × {count}" if count > 1 else ""
        parts.append(f"{_usd(hourly)}/hour{times} → ~{_usd(hourly * count * HOURS_PER_MONTH)}/month")
        basis.append("on-demand Linux" if inputs["kind"] == "ec2" else "on-demand, instance only")
    volumes = inputs.get("volumes", [])
    if volumes:
        rates = [prices.get(f"ebs:{volume_type}") for _, volume_type in volumes]
        if None not in rates:
            monthly = sum(size * rate for (size, _), rate in zip(volumes, rates)) * count
            sizes = ", ".join(f"{size} GB {volume_type}" for size, volume_type in volumes)
            iops = " + provisioned IOPS" if any(t in ("io1", "io2") for _, t in volumes) else ""
            parts.append(f"EBS {sizes}{' each' if count > 1 else ''} ~{_usd(monthly)}/month{iops}")
        elif not parts:
            return None
    if not parts:
        return None
    basis.append(region if region_given else f"{region}, region not given")
    return f"Price List ({', '.join(basis)}): {' + '.join(parts)}"


def annotate(items, env: dict) -> None:
    """Set `price` on the items that carry price inputs. Needs `aws` on the PATH given in `env`."""
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
        try:
            return _lookup(*query, aws, env)
        except Exception:  # no credentials, no network, a timeout: just no price
            return None

    with ThreadPoolExecutor(max_workers=max(1, len(queries))) as pool:
        results = dict(zip(queries, pool.map(run, queries.values())))
    for item in todo:
        region, given = regions[id(item)]
        prices = {name: results[json.dumps(query, sort_keys=True)]
                  for name, query in _queries(item.pricing, region).items()}
        item.price = _describe(item.pricing, prices, region, given) or ""
