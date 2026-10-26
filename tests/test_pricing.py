import json
import os
import tempfile
import unittest
from pathlib import Path

from costaware.classifier import classify_command, classify_operation
from costaware.hook import run
from costaware.pricing import _unit_price, annotate


def product(usd, usagetype="BoxUsage:m5.2xlarge"):
    return json.dumps({"product": {"attributes": {"usagetype": usagetype}},
                       "terms": {"OnDemand": {"t": {"priceDimensions": {"d": {"pricePerUnit": {"USD": str(usd)}}}}}}})


class FakeAws:
    """An `aws` on a temporary PATH that answers Price List queries: instances $0.46/hour, EBS $0.0952/GB-month."""

    def __init__(self, exit_code=0):
        self.dir = tempfile.TemporaryDirectory()
        instance = json.dumps({"PriceList": [product(0.46), product(0.46, "UnusedBox:db.m5.large")]})
        storage = json.dumps({"PriceList": [product(0.0952, "EBS:VolumeUsage.gp3")]})
        script = Path(self.dir.name, "aws")
        script.write_text("#!/bin/sh\n"
                          f"echo \"$*\" >> '{self.dir.name}/calls'\n"
                          f"[ {exit_code} -ne 0 ] && exit {exit_code}\n"
                          f"case \"$*\" in *productFamily*) printf '%s' '{storage}';; *) printf '%s' '{instance}';; esac\n")
        script.chmod(0o755)
        self.env = {"PATH": self.dir.name, "COSTAWARE_CACHE_DIR": os.path.join(self.dir.name, "cache"),
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


class UnitPriceTest(unittest.TestCase):
    def test_skips_unused_reservations(self):
        self.assertEqual(_unit_price([product(0.021, "InstanceUsage:db.t3.micro"), product(0.5, "UnusedBox:db.t3.micro")]), 0.021)

    def test_ambiguous_match_has_no_price(self):
        self.assertIsNone(_unit_price([product(0.46), product(0.52)]))
        self.assertIsNone(_unit_price([]))


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

    def test_no_aws_cli(self):
        [item] = items("aws ec2 run-instances --instance-type m5.2xlarge")
        annotate([item], {"PATH": ""})
        self.assertEqual(item.price, "")


class HookPriceTest(unittest.TestCase):
    command = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "aws ec2 run-instances --instance-type m5.2xlarge --region eu-central-1"}}

    def test_prompt_shows_the_price(self):
        with FakeAws() as aws:
            reason = run(self.command, aws.env)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("  Price List (on-demand Linux, eu-central-1): $0.46/hour → ~$336/month", reason)

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


if __name__ == "__main__":
    unittest.main()
