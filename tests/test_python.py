import os
import tempfile
import textwrap
import unittest

from costaware.classifier import classify_command, classify_python
from costaware.parser import find_python_sources
from costaware.pyscan import find_boto3_calls


def calls(code, **kwargs):
    return [(c.service, c.operation) for c in find_boto3_calls(textwrap.dedent(code), **kwargs)]


def levels(code, **kwargs):
    return [i.level for i in classify_python(textwrap.dedent(code), **kwargs).items]


class FindBoto3CallsTest(unittest.TestCase):
    def test_client_variable(self):
        self.assertEqual(calls("""
            import boto3
            ec2 = boto3.client("ec2", region_name="eu-west-1")
            ec2.describe_instances()
        """), [("ec2", "describe_instances")])

    def test_inline_and_session_clients(self):
        self.assertEqual(calls("""
            import boto3
            boto3.client("sts").get_caller_identity()
            session = boto3.Session(profile_name="p")
            session.client(service_name="rds").create_db_instance(DBInstanceIdentifier="d")
        """), [("sts", "get_caller_identity"), ("rds", "create_db_instance")])

    def test_client_used_before_assignment_in_function(self):
        self.assertEqual(calls("""
            import boto3
            def setup():
                ec2.create_nat_gateway(SubnetId="s")
            ec2 = boto3.client("ec2")
        """), [("ec2", "create_nat_gateway")])

    def test_attribute_targets(self):
        self.assertEqual(calls("""
            import boto3
            class Infra:
                def __init__(self):
                    self.eks = boto3.client("eks")
                def up(self):
                    self.eks.create_cluster(name="c")
        """), [("eks", "create_cluster")])

    def test_paginator_waiter_and_local_methods(self):
        self.assertEqual(calls("""
            import boto3
            s3 = boto3.client("s3")
            s3.get_paginator("list_objects_v2")
            s3.get_waiter("bucket_exists")
            s3.generate_presigned_url("get_object", Params={})
        """), [("s3", "list_objects_v2")])

    def test_unresolved_receiver_only_in_aws_context(self):
        self.assertEqual(calls("db.create_table('t')"), [])
        self.assertEqual(calls("db.create_table('t')", aws_context=True), [(None, "create_table")])
        self.assertEqual(calls("import boto3\nhelper.run_instances()"), [(None, "run_instances")])

    def test_syntax_error(self):
        from costaware.parser import ParseError
        with self.assertRaises(ParseError):
            find_boto3_calls("def (")


class ClassifyPythonTest(unittest.TestCase):
    def test_levels_follow_the_rule_table(self):
        self.assertEqual(levels("""
            import boto3
            ec2 = boto3.client("ec2")
            ec2.describe_instances()
            ec2.run_instances(ImageId="ami-1", DryRun=True)
            ec2.create_nat_gateway(SubnetId="s")
        """), ["FREE", "FREE", "HIGH"])

    def test_literal_arguments_drive_rules(self):
        self.assertEqual(levels("""
            import boto3
            ddb = boto3.client("dynamodb")
            ddb.create_table(TableName="t", BillingMode="PAY_PER_REQUEST")
            ddb.create_table(TableName="t")
        """), ["LOW", "HIGH"])

    def test_runtime_arguments_assume_the_costliest_case(self):
        result = classify_python('import boto3\nboto3.client("ec2").create_vpc_endpoint(VpcEndpointType=kind)')
        self.assertEqual((result.level, result.items[0].confidence), ("HIGH", "low"))
        # A runtime DryRun can only lower the cost, so it doesn't make the call look riskier.
        self.assertEqual(levels('import boto3\nboto3.client("ec2").run_instances(DryRun=flag)'), ["HIGH"])
        self.assertEqual(levels('import boto3\nboto3.client("ec2").create_nat_gateway(SubnetId=s)'), ["HIGH"])

    def test_s3_transfer_helpers(self):
        result = classify_python('import boto3\nboto3.client("s3").upload_file("a", "b", "k")')
        self.assertEqual((result.items[0].service, result.items[0].operation), ("s3api", "put-object"))

    def test_resource_api_is_flagged(self):
        self.assertEqual(levels("""
            import boto3
            ec2 = boto3.resource("ec2")
            ec2.Instance("i-1")
            ec2.create_instances(ImageId="ami-1", MinCount=1, MaxCount=1)
        """), ["UNKNOWN"])

    def test_dynamic_service_and_getattr(self):
        self.assertEqual(levels('import boto3\nboto3.client(name).create_thing()'), ["UNKNOWN"])
        self.assertEqual(levels('import boto3\nc = boto3.client("ec2")\ngetattr(c, op)()'), ["UNKNOWN"])

    def test_code_without_aws_is_free(self):
        self.assertEqual(classify_python("print(sum(range(10)))").level, "FREE")
        self.assertEqual(classify_python("def (").level, "FREE")
        self.assertEqual(classify_python("import boto3\ndef (").level, "UNKNOWN")


class SandboxScriptTest(unittest.TestCase):
    """The AWS MCP Server's aws___run_script sandbox: `await call_boto3(...)`, no boto3 clients."""

    def test_call_boto3(self):
        self.assertEqual(levels("""
            regions = ["eu-central-1", "eu-west-1"]
            await asyncio.gather(*[call_boto3(service_name="ec2", operation_name="DescribeVpcs", region_name=r)
                                   for r in regions], return_exceptions=True)
            nat = await call_boto3(service_name="ec2", operation_name="CreateNatGateway", params={"SubnetId": s})
            result = {"nat": nat}
            result
        """), ["FREE", "HIGH"])

    def test_params_drive_argument_rules(self):
        self.assertEqual(levels("""
            await call_boto3(service_name="dynamodb", operation_name="CreateTable",
                             params={"TableName": "t", "BillingMode": "PAY_PER_REQUEST"})
            await call_boto3(service_name="ec2", operation_name="RunInstances", params={"DryRun": True})
            await call_boto3(service_name="ec2", operation_name="CreateVpcEndpoint", params=endpoint_params)
        """), ["LOW", "FREE", "HIGH"])

    def test_snake_case_fallback_and_region(self):
        result = classify_python('await call_boto3(service_name="eks", operation_name="create_cluster", '
                                 'region_name="eu-west-1", params={"name": "c"})')
        self.assertEqual((result.level, result.items[0].operation, result.items[0].region),
                         ("HIGH", "create-cluster", "eu-west-1"))

    def test_runtime_names_are_unknown(self):
        self.assertEqual(levels('await call_boto3(service_name=svc, operation_name="ListThings")'), ["UNKNOWN"])
        self.assertEqual(levels('await call_boto3(service_name="ec2", operation_name=op)'), ["UNKNOWN"])

    def test_aws_mcp_helpers(self):
        self.assertEqual(levels("""
            help(aws_mcp)
            await aws_mcp.search_functions("landing zone")
            await aws_mcp.deploy_landing_zone(name="x")
        """), ["UNKNOWN"])

    def test_hook_on_run_script(self):
        from costaware.hook import run
        code = 'nat = await call_boto3(service_name="ec2", operation_name="CreateNatGateway", params={"SubnetId": "s"})'
        payload = {"tool_name": "mcp__aws-mcp__aws___run_script", "tool_input": {"code": code}}
        self.assertEqual(run(payload, {})["hookSpecificOutput"]["permissionDecision"], "ask")
        code = 'result = await call_boto3(service_name="ec2", operation_name="DescribeInstances")\nresult'
        payload["tool_input"]["code"] = code
        self.assertIsNone(run(payload, {}))


class ShellPythonTest(unittest.TestCase):
    def test_finds_python_sources(self):
        sources = find_python_sources("python3 -c 'import boto3' && python -u tools/x.py -v && python -m pytest")
        self.assertEqual([(s.kind, s.value) for s in sources], [("inline", "import boto3"), ("file", "tools/x.py")])

    def test_heredoc(self):
        command = "cd infra && python3 - <<'PY'\nimport boto3\nboto3.client('eks').create_cluster(name=\"it's\")\nPY\necho ok"
        self.assertEqual(classify_command(command).level, "HIGH")

    def test_heredoc_not_fed_to_python_is_shell(self):
        self.assertEqual(classify_command("bash <<EOF\naws ec2 allocate-address\nEOF").level, "HIGH")
        # Python elsewhere on the line doesn't turn a shell heredoc into Python.
        self.assertEqual(classify_command("python3 -V; bash <<EOF\naws ec2 allocate-address\nEOF").level, "HIGH")

    def test_heredoc_bodies_cannot_hide_commands(self):
        # A quote in a heredoc body must not swallow the commands after it.
        self.assertEqual(classify_command("cat > notes.txt <<EOF\nwe'll see\nEOF\naws ec2 allocate-address").level,
                         "HIGH")
        self.assertEqual(classify_command("ssh bastion <<EOF\naws ec2 allocate-address\nEOF").level, "HIGH")
        self.assertEqual(classify_command("bash <<EOF\necho it's\naws ec2 allocate-address\nEOF").level, "UNKNOWN")
        # Any body might run as shell (ksh, fish, xargs...), so an unparsable one that mentions aws fails closed.
        self.assertEqual(classify_command("ksh <<EOF\necho it's\naws ec2 allocate-address\nEOF").level, "UNKNOWN")
        self.assertEqual(classify_command("cat > notes.md <<'EOF'\nDon't touch AWS\nEOF").level, "FREE")

    def test_heredoc_belongs_to_its_own_command(self):
        # A Python/shell polyglot fed to bash, with python3 elsewhere on the line, still runs `aws`.
        polyglot = '""":"\naws ec2 allocate-address\n":"""'
        self.assertEqual(classify_command(f"echo python3; bash <<EOF\n{polyglot}\nEOF").level, "HIGH")
        # A `<<` inside quotes is not a heredoc; the lines after it are commands.
        self.assertEqual(classify_command(f'echo "python3 <<X"\n{polyglot}\nX').level, "HIGH")
        # Genuine Python mentioning aws in comments or strings stays quiet.
        script = "import boto3\n# don't call aws by hand\nprint(\"it's aws\")\nboto3.client('ec2').describe_instances()"
        self.assertEqual(classify_command(f"cat <<EOF | python3\n{script}\nEOF").level, "FREE")

    def test_heredoc_reader_guess_cannot_hide_calls(self):
        py = "import boto3\nboto3.client('ec2').allocate_address()"
        for command in [f"python3 - 2>&1 <<EOF\n{py}\nEOF", f"(python3 - <<EOF\n{py}\nEOF\n)",
                        f"if true; then python3 - <<EOF\n{py}\nEOF\nfi", f"some-runner <<EOF\n{py}\nEOF",
                        "python3 - <<EOF\necho '\nx'; aws ec2 allocate-address; echo 'y\n'\nEOF"]:
            self.assertEqual(classify_command(command).level, "HIGH", command)

    def test_genuine_python_heredoc_stays_quiet(self):
        script = ("import boto3\n# don't\nr = 'arn:aws:iam::1:role/x'\naws = boto3.client('sts')\n"
                  "aws.get_caller_identity()\nprint(\"it's\")")
        self.assertEqual(classify_command(f"python3 - <<'PY'\n{script}\nPY").level, "FREE")

    def test_heredoc_piped_into_python(self):
        command = "cat <<EOF | python3\nimport boto3\nboto3.client('ec2').allocate_address()\nEOF"
        self.assertEqual(classify_command(command).level, "HIGH")

    def test_python_dash_c(self):
        command = """python -c "import boto3; boto3.client('ec2').allocate_address()" """
        self.assertEqual(classify_command(command).level, "HIGH")

    def test_script_file_resolved_against_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "scripts"))
            with open(os.path.join(tmp, "scripts", "up.py"), "w") as f:
                f.write("import boto3\nboto3.client('rds').create_db_instance(DBInstanceIdentifier='d')\n")
            result = classify_command("python3 scripts/up.py", cwd=tmp)
        self.assertEqual(result.level, "HIGH")
        self.assertTrue(result.items[0].command.startswith("scripts/up.py: "))

    def test_missing_script_is_ignored(self):
        self.assertEqual(classify_command("python3 does_not_exist.py", cwd=tempfile.gettempdir()).level, "FREE")

    def test_mixed_shell_and_python(self):
        command = "aws sts get-caller-identity && python3 -c 'import boto3; boto3.client(\"s3\").list_buckets()'"
        self.assertEqual([i.level for i in classify_command(command).items], ["FREE", "LOW"])


if __name__ == "__main__":
    unittest.main()
