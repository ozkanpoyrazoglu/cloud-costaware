import json
import os
import tempfile
import unittest
from pathlib import Path

from costaware.classifier import apply_prices, classify_command, classify_operation
from costaware.cli import main as cli_main
from costaware.hook import run
from costaware.pricing import _unit_price, annotate


def product(usd, usagetype="BoxUsage:m5.2xlarge", tiers=None):
    dimensions = {f"d{i}": {"pricePerUnit": {"USD": str(price)}} for i, price in enumerate(tiers or [usd])}
    return json.dumps({"product": {"attributes": {"usagetype": usagetype}},
                       "terms": {"OnDemand": {"t": {"priceDimensions": dimensions}}}})


# Price List answers by a text found in the query: instances $0.46/hour, EBS $0.0952/GB-month, and the components
# the rule table names. The first match wins, so the specific patterns come first.
ANSWERS = [
    ("NatGateway-Hours", [product(0.045, "EUC1-NatGateway-Hours")]),
    ("NatGateway-Bytes", [product(0.045, "EUC1-NatGateway-Bytes")]),
    ("PublicIPv4", [product(0.005, "EUC1-PublicIPv4:IdleAddress")]),
    ("VpcEndpoint-Hours", [product(0.01, "EUC1-VpcEndpoint-Hours")]),
    ("VpcEndpoint-Bytes", [product(0.01, "EUC1-VpcEndpoint-Bytes")]),
    ("CW:AlarmMonitorUsage", [product(0.1, "EUC1-CW:AlarmMonitorUsage")]),
    ("S3-API-Tier1", [product(0.000005, "EUC1-Requests-Tier1")]),
    ("DataScannedInTB", [product(5.0, "EUC1-DataScannedInTB")]),
    ("SQS-APIRequest-Tier1", [product(0.0000004, "EUC1-Requests-Tier1"), product(0.0000005, "EUC1-Requests-FIFO-Tier1")]),
    ("productFamily", [product(0.0952, "EBS:VolumeUsage.gp3")]),
    ("", [product(0.46), product(0.46, "UnusedBox:db.m5.large")]),
]


class FakeAws:
    """An `aws` on a temporary PATH that answers Price List queries from ANSWERS."""

    def __init__(self, exit_code=0, answers=ANSWERS, next_token=False):
        self.dir = tempfile.TemporaryDirectory()
        cases = []
        for index, (pattern, products) in enumerate(answers):
            body = {"PriceList": products, **({"NextToken": "more"} if next_token else {})}
            Path(self.dir.name, f"answer{index}.json").write_text(json.dumps(body))
            cases.append(f"*{pattern}*) cat '{self.dir.name}/answer{index}.json';;")
        script = Path(self.dir.name, "aws")
        script.write_text("#!/bin/sh\n"
                          f"echo \"$*\" >> '{self.dir.name}/calls'\n"
                          f"[ {exit_code} -ne 0 ] && exit {exit_code}\n"
                          f"case \"$*\" in {' '.join(cases)} esac\n")
        script.chmod(0o755)
        self.env = {"PATH": f"{self.dir.name}:/bin:/usr/bin", "COSTAWARE_CACHE_DIR": os.path.join(self.dir.name, "cache"),
                    "COSTAWARE_CONFIG": os.path.join(self.dir.name, "none.json"), "HOME": self.dir.name}

    @property
    def calls(self):
        path = Path(self.dir.name, "calls")
        return path.read_text().splitlines() if path.exists() else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.dir.cleanup()


def items(command):
    return classify_command(command).items


def priced(command, env, **config):
    return apply_prices(classify_command(command), env, config or None)


class PriceInputsTest(unittest.TestCase):
    def test_run_instances_from_cli(self):
        [item] = items("aws ec2 run-instances --instance-type t3.micro --count 2:5"
                       " --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=30,VolumeType=gp3}'")
        self.assertEqual(item.pricing, {"kind": "ec2", "instance_type": "t3.micro", "count": 5, "volumes": [(30, "gp3")]})

    def test_run_instances_from_api_params(self):
        item = classify_operation("ec2", "RunInstances", {
            "InstanceType": "m5.2xlarge", "MinCount": 1, "MaxCount": 2,
            "BlockDeviceMappings": [{"Ebs": {"VolumeSize": 30, "VolumeType": "gp3"}}, {"Ebs": {"VolumeSize": 8}}]}).items[0]
        self.assertEqual(item.pricing, {"kind": "ec2", "instance_type": "m5.2xlarge", "count": 2, "volumes": [(30, "gp3")]})

    def test_nothing_to_price(self):
        self.assertEqual(items("aws ec2 run-instances --launch-template LaunchTemplateName=x")[0].pricing, {})
        self.assertEqual(items("aws ec2 request-spot-instances --instance-count 2")[0].pricing, {})
        self.assertEqual(items("aws rds create-db-instance --engine oracle-ee --db-instance-class db.m5.large")[0].pricing, {})

    def test_rds(self):
        [item] = items("aws rds create-db-instance --engine postgres --db-instance-class db.r6g.large --multi-az")
        self.assertEqual(item.pricing, {"kind": "rds", "instance_class": "db.r6g.large", "engine": "PostgreSQL",
                                        "multi_az": True})

    def test_components_count_argument_values(self):
        for subnets in ("--subnet-ids s-1 s-2", "--subnet-ids s-1,s-2", "--subnet-ids '[\"s-1\", \"s-2\"]'"):
            [item] = items(f"aws ec2 create-vpc-endpoint --vpc-endpoint-type Interface --vpc-id v {subnets}")
            self.assertEqual([c["quantity"] for c in item.pricing["components"]], [2, 1], subnets)


class UnitPriceTest(unittest.TestCase):
    def test_skips_unused_reservations(self):
        self.assertEqual(_unit_price([product(0.021, "InstanceUsage:db.t3.micro"), product(0.5, "UnusedBox:db.t3.micro")]), 0.021)

    def test_ambiguous_match_has_no_price(self):
        self.assertIsNone(_unit_price([product(0.46), product(0.52)]))
        self.assertIsNone(_unit_price([]))

    def test_tiers_count_at_the_highest_rate(self):
        self.assertEqual(_unit_price([product(None, "HostedZone", tiers=[0.5, 0.1])]), 0.5)
        self.assertEqual(_unit_price([product(None, "Requests-Tier1", tiers=[0, 0.0000005])]), 0.0000005)

    def test_other_currencies_are_skipped(self):
        cny = json.dumps({"product": {"attributes": {"usagetype": "CNN1-APIRequest"}},
                          "terms": {"OnDemand": {"t": {"priceDimensions": {"d": {"pricePerUnit": {"CNY": "0.07"}}}}}}})
        self.assertEqual(_unit_price([product(0.01, "USE1-APIRequest"), cny]), 0.01)

    def test_exclusions(self):
        products = [product(0.0000004, "EUC1-Requests-Tier1"), product(0.0000005, "EUC1-Requests-FIFO-Tier1")]
        self.assertIsNone(_unit_price(products))
        self.assertEqual(_unit_price(products, (("usagetype", ("FIFO", "Fair")),)), 0.0000004)


class AnnotateTest(unittest.TestCase):
    def test_price_line_and_cache(self):
        with FakeAws() as aws:
            [item] = items("aws ec2 run-instances --instance-type m5.2xlarge --region eu-central-1"
                           " --block-device-mappings '[{\"Ebs\": {\"VolumeSize\": 30, \"VolumeType\": \"gp3\"}}]'")
            annotate([item], aws.env)
            self.assertEqual(item.price, "Price List (on-demand Linux, eu-central-1):"
                                         " $0.46/hour → ~$336/month + EBS 30 GB gp3 ~$2.86/month")
            self.assertEqual(len(aws.calls), 2)
            annotate([item], aws.env)
            self.assertEqual(len(aws.calls), 2)

    def test_count_and_missing_region(self):
        with FakeAws() as aws:
            [item] = items("aws ec2 run-instances --instance-type m5.2xlarge --count 3")
            annotate([item], aws.env)
            self.assertEqual(item.price, "Price List (on-demand Linux, us-east-1, region not given):"
                                         " $0.46/hour × 3 → ~$1,007/month")

    def test_failures_leave_the_price_out_and_are_not_cached(self):
        with FakeAws(exit_code=255) as aws:
            [item] = items("aws ec2 run-instances --instance-type m5.2xlarge --region eu-central-1")
            annotate([item], aws.env)
            annotate([item], aws.env)
            self.assertEqual((item.price, len(aws.calls)), ("", 2))

    def test_more_than_one_page_is_ambiguous(self):
        with FakeAws(next_token=True) as aws:
            [item] = items("aws ec2 run-instances --instance-type m5.2xlarge --region eu-central-1")
            annotate([item], aws.env)
            self.assertEqual(item.price, "")

    def test_no_aws_cli(self):
        [item] = items("aws ec2 run-instances --instance-type m5.2xlarge")
        annotate([item], {"PATH": ""})
        self.assertEqual(item.price, "")

    def test_components(self):
        with FakeAws() as aws:
            [item] = items("aws ec2 create-nat-gateway --subnet-id s-1 --region eu-central-1")
            annotate([item], aws.env)
            self.assertEqual(item.price, "Price List (on-demand, eu-central-1):"
                                         " $0.045/hour → ~$32.85/month + $0.045/GB processed")
            self.assertAlmostEqual(item.monthly_usd, 32.85)
            filters = [f for call in aws.calls for f in json.loads(call.split("--filters ", 1)[1])]  # queries run at once
            self.assertIn({"Type": "CONTAINS", "Field": "usagetype", "Value": "NatGateway-Hours"}, filters)
            self.assertIn({"Type": "TERM_MATCH", "Field": "regionCode", "Value": "eu-central-1"}, filters)

    def test_per_request_prices_are_scaled(self):
        with FakeAws() as aws:
            [item] = items("aws s3 ls s3://bucket --region eu-central-1")
            annotate([item], aws.env)
            self.assertEqual(item.price, "Price List (on-demand, eu-central-1): $0.005/1,000 LIST requests")
            self.assertIsNone(item.monthly_usd)

    def test_quantity(self):
        with FakeAws() as aws:
            [item] = items("aws ec2 create-vpc-endpoint --vpc-endpoint-type Interface --vpc-id v"
                           " --subnet-ids s-1 s-2 s-3 --region eu-central-1")
            annotate([item], aws.env)
            self.assertEqual(item.price, "Price List (on-demand, eu-central-1):"
                                         " $0.01/hour × 3 → ~$21.90/month + $0.01/GB processed")

    def test_exclusions_reach_the_lookup(self):
        with FakeAws() as aws:
            [item] = items("aws sqs send-message --queue-url u --message-body b --region eu-central-1")
            annotate([item], aws.env)
            self.assertEqual(item.price, "Price List (on-demand, eu-central-1): $0.4/1M requests (standard queue)")


class LevelFromPriceTest(unittest.TestCase):
    def test_cheap_resource_is_low(self):
        with FakeAws() as aws:
            result = priced("aws cloudwatch put-metric-alarm --alarm-name a --region eu-central-1", aws.env)
        self.assertEqual(result.level, "LOW")
        self.assertEqual(result.items[0].price, "Price List (on-demand, eu-central-1): $0.1/month")

    def test_threshold(self):
        with FakeAws() as aws:
            self.assertEqual(priced("aws ec2 allocate-address", aws.env).level, "HIGH")  # ~$3.65/month
            self.assertEqual(priced("aws ec2 allocate-address", aws.env, high_monthly_usd=5.0).level, "LOW")
            self.assertEqual(priced("aws cloudwatch put-metric-alarm --alarm-name a", aws.env,
                                    high_monthly_usd=0.05).level, "HIGH")

    def test_missing_price_stays_high(self):
        with FakeAws(exit_code=255) as aws:
            self.assertEqual(priced("aws cloudwatch put-metric-alarm --alarm-name a", aws.env).level, "HIGH")
        self.assertEqual(priced("aws cloudwatch put-metric-alarm --alarm-name a", {"PATH": ""}).level, "HIGH")

    def test_partial_estimates_never_lower_the_level(self):
        with FakeAws() as aws:
            # A t-shirt-sized volume is complete on its own; an instance's root volume and storage aren't known.
            self.assertEqual(priced("aws ec2 create-volume --size 1 --volume-type gp3 --availability-zone a", aws.env).level, "LOW")
            self.assertEqual(priced("aws ec2 run-instances --instance-type m5.2xlarge", aws.env,
                                    high_monthly_usd=10000).level, "HIGH")

    def test_size_dependent_calls_show_a_unit_price_and_stay_unknown(self):
        with FakeAws() as aws:
            result = priced("aws athena start-query-execution --query-string 'SELECT 1' --region eu-central-1", aws.env)
        self.assertEqual(result.level, "UNKNOWN")
        self.assertEqual(result.items[0].price, "Price List (on-demand, eu-central-1): $5.00/TB scanned")

    def test_free_calls_are_not_looked_up(self):
        with FakeAws() as aws:
            priced("aws ec2 describe-instances", aws.env)
            self.assertEqual(aws.calls, [])


class HookPriceTest(unittest.TestCase):
    command = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "aws ec2 run-instances --instance-type m5.2xlarge --region eu-central-1"}}

    def test_prompt_shows_the_price(self):
        with FakeAws() as aws:
            reason = run(self.command, aws.env)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("  Price List (on-demand Linux, eu-central-1): $0.46/hour → ~$336/month", reason)

    def test_low_calls_get_the_price_too(self):
        with FakeAws() as aws:
            payload = {**self.command, "tool_input": {"command": "aws s3 ls s3://b --region eu-central-1"}}
            context = run(payload, aws.env)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Price List (on-demand, eu-central-1): $0.005/1,000 LIST requests", context)

    def test_a_cheap_resource_runs_without_a_prompt(self):
        with FakeAws() as aws:
            payload = {**self.command, "tool_input": {"command": "aws cloudwatch put-metric-alarm --alarm-name a"}}
            output = run(payload, aws.env)["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", output)
        self.assertIn("costaware: LOW", output["additionalContext"])

    def test_env_config_changes_threshold(self):
        with FakeAws() as aws:
            Path(aws.env["COSTAWARE_CONFIG"]).write_text(json.dumps({"high_monthly_usd": 5.0}))
            payload = {**self.command, "tool_input": {"command": "aws ec2 allocate-address"}}
            self.assertNotIn("permissionDecision", run(payload, aws.env)["hookSpecificOutput"])

    def test_project_config_can_only_tighten_the_threshold(self):
        with FakeAws() as aws, tempfile.TemporaryDirectory() as project:
            Path(project, ".costaware.json").write_text(json.dumps({"high_monthly_usd": 0.05}))
            payload = {**self.command, "cwd": project,
                       "tool_input": {"command": "aws cloudwatch put-metric-alarm --alarm-name a"}}
            output = run(payload, {**aws.env, "CLAUDE_PROJECT_DIR": project})["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")

    def test_price_lookup_can_be_turned_off(self):
        with FakeAws() as aws:
            Path(aws.env["COSTAWARE_CONFIG"]).write_text('{"price_lookup": false}')
            reason = run(self.command, aws.env)["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertNotIn("Price List", reason)
            self.assertEqual(aws.calls, [])

    def test_project_config_cannot_turn_it_off(self):
        with FakeAws() as aws, tempfile.TemporaryDirectory() as project:
            Path(project, ".costaware.json").write_text('{"price_lookup": false}')
            payload = {**self.command, "cwd": project}
            reason = run(payload, {**aws.env, "CLAUDE_PROJECT_DIR": project})["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Price List", reason)


class CliPriceTest(unittest.TestCase):
    def run_cli(self, env, *args):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest import mock
        out = StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out):
            cli_main(["classify", "--format", "json", *args])
        return json.loads(out.getvalue())

    def test_cli_looks_up_prices_like_the_hook(self):
        with FakeAws() as aws:
            self.assertEqual(self.run_cli(aws.env, "aws cloudwatch put-metric-alarm --alarm-name a")["level"], "LOW")
            self.assertEqual(self.run_cli(aws.env, "--no-price", "aws cloudwatch put-metric-alarm --alarm-name a")["level"], "HIGH")


if __name__ == "__main__":
    unittest.main()
