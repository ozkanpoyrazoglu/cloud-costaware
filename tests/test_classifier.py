import json
import unittest

from costaware.classifier import classify_command, classify_operation, load_rules

VALID_COSTS = {"free", "request", "usage", "recurring", "one_time", "variable"}


def level(command, **config):
    return classify_command(command, config=config or None).level


class RuleTableTest(unittest.TestCase):
    def test_every_rule_is_well_formed(self):
        rules = load_rules()
        entries = list(rules["global_rules"])
        for name, service in rules["services"].items():
            entries += service.get("rules", [])
            for key in ("default_read", "default_write"):
                if key in service:
                    self.assertIn(service[key]["cost"], VALID_COSTS, f"{name}.{key}")
        for rule in entries:
            self.assertIn("op", rule)
            self.assertIn(rule["cost"], VALID_COSTS, rule)


class ClassifyCommandTest(unittest.TestCase):
    def test_no_aws_call_is_free(self):
        result = classify_command("ls -la")
        self.assertEqual((result.level, result.items), ("FREE", []))

    def test_free_reads(self):
        self.assertEqual(level("aws sts get-caller-identity"), "FREE")
        self.assertEqual(level("aws ec2 describe-instances --region eu-west-1"), "FREE")
        self.assertEqual(level("aws iam list-roles"), "FREE")
        self.assertEqual(level("aws athena get-query-results --query-execution-id x"), "FREE")

    def test_reads_that_are_not_free(self):
        self.assertEqual(level("aws ce get-cost-and-usage --time-period Start=2026-01-01,End=2026-02-01"), "LOW")
        self.assertEqual(level("aws cloudwatch get-metric-data --cli-input-json file://q.json"), "LOW")
        self.assertEqual(level("aws s3 ls s3://bucket --recursive"), "LOW")
        self.assertEqual(level("aws s3api get-object --bucket b --key k out"), "LOW")
        self.assertEqual(level("aws dynamodb scan --table-name t"), "UNKNOWN")

    def test_local_commands(self):
        self.assertEqual(level("aws --version"), "FREE")
        self.assertEqual(level("aws configure list"), "FREE")
        self.assertEqual(level("aws ec2 run-instances help"), "FREE")
        self.assertEqual(level("aws ec2 run-instances --generate-cli-skeleton"), "FREE")

    def test_resources_that_burn_money_while_idle(self):
        self.assertEqual(level("aws ec2 run-instances --image-id ami-1 --instance-type t3.micro"), "HIGH")
        self.assertEqual(level("aws ec2 create-nat-gateway --subnet-id s-1"), "HIGH")
        self.assertEqual(level("aws ec2 allocate-address"), "HIGH")
        self.assertEqual(level("aws eks create-cluster --name c"), "HIGH")
        self.assertEqual(level("aws rds create-db-instance --db-instance-identifier d"), "HIGH")

    def test_dry_run_is_free(self):
        self.assertEqual(level("aws ec2 run-instances --image-id ami-1 --dry-run"), "FREE")

    def test_recurring_cost_is_high_until_a_price_says_otherwise(self):
        # The rule table holds no prices; a looked-up price can lower these to LOW (see test_pricing).
        self.assertEqual(level("aws cloudwatch put-metric-alarm --alarm-name a"), "HIGH")
        self.assertEqual(level("aws secretsmanager create-secret --name s"), "HIGH")
        self.assertEqual(level("aws cloudwatch put-dashboard --dashboard-name d"), "HIGH")

    def test_no_rule_holds_a_price(self):
        rules = json.dumps(load_rules()["services"])
        self.assertNotIn("monthly_usd", rules)
        self.assertNotRegex(rules, r"\$\d")

    def test_argument_aware_rules(self):
        self.assertEqual(level("aws dynamodb create-table --table-name t --billing-mode PAY_PER_REQUEST"), "LOW")
        self.assertEqual(level("aws dynamodb create-table --table-name t"), "HIGH")
        self.assertEqual(level("aws ec2 create-vpc-endpoint --vpc-id v --service-name com.amazonaws.us-east-1.s3"), "FREE")
        self.assertEqual(level("aws ec2 create-vpc-endpoint --vpc-endpoint-type Interface --vpc-id v"), "HIGH")
        self.assertEqual(level("aws ssm put-parameter --name p --value v --tier Advanced"), "HIGH")
        self.assertEqual(level("aws ssm put-parameter --name p --value v"), "FREE")

    def test_s3_transfer_direction(self):
        self.assertEqual(level("aws s3 cp ./file s3://b/file"), "LOW")
        self.assertEqual(level("aws s3 sync s3://b/data ./data"), "UNKNOWN")

    def test_size_dependent_operations_are_unknown(self):
        self.assertEqual(level("aws athena start-query-execution --query-string 'SELECT * FROM t'"), "UNKNOWN")
        self.assertEqual(level("aws logs start-query --log-group-name g --query-string q"), "UNKNOWN")
        self.assertEqual(level("aws cloudformation deploy --template-file t.yaml --stack-name s"), "UNKNOWN")

    def test_delete_is_free_but_data_plane_deletes_are_not(self):
        self.assertEqual(level("aws ec2 delete-nat-gateway --nat-gateway-id n"), "FREE")
        self.assertEqual(level("aws ec2 terminate-instances --instance-ids i-1"), "FREE")
        self.assertEqual(level("aws dynamodb delete-item --table-name t --key k"), "LOW")
        self.assertEqual(level("aws sqs delete-message --queue-url u --receipt-handle h"), "LOW")

    def test_unknown_service(self):
        self.assertEqual(level("aws apigateway get-rest-apis"), "UNKNOWN")
        self.assertEqual(level("aws apigateway get-rest-apis", unknown_service_read="LOW"), "LOW")
        self.assertEqual(level("aws apigateway create-rest-api --name x", unknown_service_read="LOW"), "UNKNOWN")

    def test_unknown_operation_in_known_service_without_default(self):
        self.assertEqual(level("aws ce get-cost-forecast"), "LOW")
        self.assertEqual(level("aws bedrock-runtime converse --model-id m"), "UNKNOWN")

    def test_most_severe_call_wins(self):
        result = classify_command("aws sts get-caller-identity && aws s3 ls && aws ec2 run-instances --image-id a")
        self.assertEqual(result.level, "HIGH")
        self.assertEqual([i.level for i in result.items], ["FREE", "LOW", "HIGH"])

    def test_unparsable_command(self):
        self.assertEqual(level("aws s3 cp 'unterminated"), "UNKNOWN")
        self.assertEqual(level("echo 'unterminated"), "FREE")


class ClassifyOperationTest(unittest.TestCase):
    def test_api_style_names(self):
        self.assertEqual(classify_operation("ec2", "RunInstances").level, "HIGH")
        self.assertEqual(classify_operation("ec2", "RunInstances", {"DryRun": True}).level, "FREE")
        self.assertEqual(classify_operation("rds", "DescribeDBInstances").level, "FREE")

    def test_boto3_s3_maps_to_s3api(self):
        result = classify_operation("s3", "ListObjectsV2", {"Bucket": "b"})
        self.assertEqual((result.items[0].service, result.level), ("s3api", "LOW"))

    def test_params_drive_argument_rules(self):
        self.assertEqual(classify_operation("dynamodb", "CreateTable", {"BillingMode": "PAY_PER_REQUEST"}).level, "LOW")
        self.assertEqual(classify_operation("dynamodb", "CreateTable", {"BillingMode": "PROVISIONED"}).level, "HIGH")


class DetailsTest(unittest.TestCase):
    def details(self, command):
        return [d for item in classify_command(command).items for d in item.details]

    def test_plain_and_flag_arguments(self):
        self.assertEqual(self.details("aws rds create-db-instance --db-instance-identifier d --engine postgres"
                                      " --db-instance-class db.r6g.large --multi-az --allocated-storage 100"),
                         ["engine=postgres", "db-instance-class=db.r6g.large", "multi-az", "allocated-storage=100"])

    def test_fields_inside_json_and_shorthand_arguments(self):
        json_arg = """aws ec2 run-instances --instance-type t3.micro --block-device-mappings '[{"Ebs": {"VolumeSize": 30}}]'"""
        self.assertEqual(self.details(json_arg), ["instance-type=t3.micro", "volume-size=30"])
        shorthand = "aws ec2 run-instances --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=100,VolumeType=gp3}'"
        self.assertEqual(self.details(shorthand), ["volume-size=100", "volume-type=gp3"])
        self.assertEqual(self.details("aws ec2 run-instances --block-device-mappings file://bdm.json"), [])

    def test_api_params(self):
        item = classify_operation("eks", "CreateNodegroup", {"instanceTypes": ["m5.large", "m5.xlarge"],
                                                             "scalingConfig": {"desiredSize": 3, "maxSize": 6}}).items[0]
        self.assertEqual(item.details, ["instance-types=m5.large,m5.xlarge", "desired-size=3", "max-size=6"])

    def test_long_values_are_clipped(self):
        ids = " ".join(f"i-{n:017d}" for n in range(5))
        [detail] = self.details(f"aws ec2 start-instances --instance-ids {ids}")
        self.assertEqual(len(detail), len("instance-ids=") + 40)
        self.assertTrue(detail.endswith("..."))


if __name__ == "__main__":
    unittest.main()
