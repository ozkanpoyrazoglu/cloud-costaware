import unittest

from costaware.parser import find_aws_calls, to_cli_name


def ops(command):
    return [(c.service, c.operation) for c in find_aws_calls(command)]


class FindAwsCallsTest(unittest.TestCase):
    def test_ignores_non_aws_commands(self):
        self.assertEqual(ops("grep aws file.txt | wc -l"), [])

    def test_chained_and_piped(self):
        command = "aws sts get-caller-identity && aws ec2 describe-instances | jq . ; aws s3 ls"
        self.assertEqual(ops(command), [("sts", "get-caller-identity"), ("ec2", "describe-instances"), ("s3", "ls")])

    def test_newline_separated_with_comment(self):
        command = "aws s3 ls # list buckets\naws ec2 run-instances --image-id ami-1"
        self.assertEqual(ops(command), [("s3", "ls"), ("ec2", "run-instances")])

    def test_env_prefix_and_wrappers(self):
        self.assertEqual(ops("AWS_PROFILE=prod sudo -E aws ec2 describe-vpcs"), [("ec2", "describe-vpcs")])
        self.assertEqual(ops("cat ids | xargs -n1 aws ec2 terminate-instances --instance-ids"),
                         [("ec2", "terminate-instances")])

    def test_global_options_before_service(self):
        [call] = find_aws_calls("aws --profile p --region eu-west-1 --no-cli-pager ec2 describe-instances")
        self.assertEqual((call.service, call.operation, call.region), ("ec2", "describe-instances", "eu-west-1"))

    def test_region_after_operation(self):
        [call] = find_aws_calls("aws ec2 describe-instances --region=us-west-2")
        self.assertEqual(call.region, "us-west-2")

    def test_redirects_are_not_arguments(self):
        [call] = find_aws_calls("aws s3 cp ./a s3://b/a 2>/dev/null")
        self.assertEqual(call.positionals, ["./a", "s3://b/a"])
        self.assertNotIn(">", call.raw)

    def test_command_substitution(self):
        self.assertEqual(ops('echo "$(aws athena start-query-execution --query-string "SELECT 1")"'),
                         [("athena", "start-query-execution")])
        self.assertEqual(ops("ID=$(aws sts get-caller-identity --query Account --output text)"),
                         [("sts", "get-caller-identity")])

    def test_shell_dash_c(self):
        self.assertEqual(ops("bash -c 'aws ec2 run-instances --image-id ami-1'"), [("ec2", "run-instances")])

    def test_line_continuation(self):
        self.assertEqual(ops("aws ec2 run-instances \\\n  --image-id ami-1 \\\n  --count 2"), [("ec2", "run-instances")])

    def test_list_valued_options(self):
        [call] = find_aws_calls("aws ec2 stop-instances --instance-ids i-1 i-2 --dry-run")
        self.assertEqual(call.options["--instance-ids"], ["i-1", "i-2"])
        self.assertEqual(call.options["--dry-run"], [])

    def test_s3_direction(self):
        def direction(command):
            return find_aws_calls(command)[0].facts.get("direction")

        self.assertEqual(direction("aws s3 cp s3://b/k ./k"), "download")
        self.assertEqual(direction("aws s3 sync --delete ./dir s3://b/dir"), "upload")
        self.assertEqual(direction("aws s3 cp --recursive s3://a/ s3://b/"), "s3-to-s3")

    def test_absolute_aws_path(self):
        self.assertEqual(ops("/usr/local/bin/aws s3 ls"), [("s3", "ls")])


class ToCliNameTest(unittest.TestCase):
    def test_conversions(self):
        self.assertEqual(to_cli_name("RunInstances"), "run-instances")
        self.assertEqual(to_cli_name("DescribeDBInstances"), "describe-db-instances")
        self.assertEqual(to_cli_name("ListObjectsV2"), "list-objects-v2")
        self.assertEqual(to_cli_name("GetMetricData"), "get-metric-data")
        self.assertEqual(to_cli_name("run-instances"), "run-instances")


if __name__ == "__main__":
    unittest.main()
