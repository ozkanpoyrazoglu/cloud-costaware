import json
import re
import tempfile
import unittest
from pathlib import Path

from costaware.hook import run


def bash(command):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}


def mcp(tool_name, **tool_input):
    return {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input}


def decision(response):
    return response and response["hookSpecificOutput"].get("permissionDecision")


class BashHookTest(unittest.TestCase):
    def test_free_command_produces_no_output(self):
        self.assertIsNone(run(bash("aws sts get-caller-identity"), {}))
        self.assertIsNone(run(bash("ls -la"), {}))

    def test_low_adds_context_without_deciding(self):
        response = run(bash("aws s3 ls s3://bucket"), {})
        output = response["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", output)
        self.assertIn("costaware: LOW", output["additionalContext"])

    def test_high_asks_with_cost_in_reason(self):
        response = run(bash("aws ec2 create-nat-gateway --subnet-id s-1"), {})
        self.assertEqual(decision(response), "ask")
        self.assertIn("ec2 create-nat-gateway · recurring cost\n  Billed hourly while it exists",
                      response["hookSpecificOutput"]["permissionDecisionReason"])

    def test_unknown_asks(self):
        self.assertEqual(decision(run(bash("aws athena start-query-execution --query-string 'SELECT 1'"), {})), "ask")

    def test_reason_lists_only_costly_calls(self):
        response = run(bash("aws sts get-caller-identity && aws eks create-cluster --name c"), {})
        reason = response["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("eks create-cluster", reason)
        self.assertNotIn("get-caller-identity", reason)


class McpHookTest(unittest.TestCase):
    def test_cli_style_tool(self):
        payload = mcp("mcp__aws-api__call_aws", cli_command="aws ec2 run-instances --image-id ami-1")
        self.assertEqual(decision(run(payload, {})), "ask")
        self.assertIsNone(run(mcp("mcp__aws-api__call_aws", cli_command="aws iam list-roles"), {}))

    def test_cli_command_list(self):
        payload = mcp("mcp__aws-api__call_aws", cli_command=["aws iam list-roles", "aws ec2 allocate-address"])
        self.assertEqual(decision(run(payload, {})), "ask")

    def test_api_style_tool(self):
        payload = mcp("mcp__aws__invoke", service="dynamodb", operation="CreateTable",
                      parameters={"TableName": "t", "BillingMode": "PAY_PER_REQUEST"})
        self.assertNotIn("permissionDecision", run(payload, {})["hookSpecificOutput"])
        payload["tool_input"]["parameters"]["BillingMode"] = "PROVISIONED"
        self.assertEqual(decision(run(payload, {})), "ask")

    def test_script_tool(self):
        code = 'import boto3\nec2 = boto3.client("ec2")\nec2.create_nat_gateway(SubnetId="s")'
        response = run(mcp("mcp__aws-mcp__aws___run_script", code=code), {})
        self.assertEqual(decision(response), "ask")
        self.assertIn("ec2 create-nat-gateway", response["hookSpecificOutput"]["permissionDecisionReason"])
        code = 'import boto3\nboto3.client("ec2").describe_instances()'
        self.assertIsNone(run(mcp("mcp__aws-mcp__aws___run_script", code=code), {}))

    def test_prompt_lists_cost_arguments_instead_of_code(self):
        code = ('resp = await call_boto3(service_name="ec2", operation_name="RunInstances", region_name="eu-central-1",'
                ' params={"ImageId": "ami-1", "InstanceType": "m5.2xlarge", "MinCount": 1, "MaxCount": 1,'
                ' "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 30, "VolumeType": "gp3"}}]})')
        reason = run(mcp("mcp__aws-mcp__aws___run_script", code=code), {})["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(reason.splitlines()[1:3], [
            "- [HIGH] ec2 run-instances · recurring cost · eu-central-1",
            "  instance-type=m5.2xlarge · max-count=1 · volume-size=30 · volume-type=gp3",
        ])
        self.assertNotIn("ami-1", reason)

    def test_prompt_locates_calls_only_when_there_are_several(self):
        code = 'import boto3\nec2 = boto3.client("ec2")\nec2.create_nat_gateway(SubnetId="s")'
        reason = run(mcp("mcp__aws-mcp__aws___run_script", code=code), {})["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertNotIn("line 3", reason)
        code += '\nec2.allocate_address()'
        reason = run(mcp("mcp__aws-mcp__aws___run_script", code=code), {})["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("ec2 create-nat-gateway · recurring cost (line 3)", reason)
        self.assertIn("(line 4)", reason)

    def test_prompt_shows_code_of_unresolved_calls(self):
        response = run(mcp("mcp__aws-mcp__aws___run_script", code="clients['eks'].create_cluster(name='c')"), {})
        self.assertIn("clients['eks'].create_cluster(name='c')", response["hookSpecificOutput"]["permissionDecisionReason"])

    def test_script_tool_with_unresolved_client(self):
        # In an AWS tool, a costly-looking call on an unknown object is treated as UNKNOWN.
        response = run(mcp("mcp__aws-mcp__aws___run_script", code="clients['eks'].create_cluster(name='c')"), {})
        self.assertEqual(decision(response), "ask")

    def test_bash_script_resolved_against_payload_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "up.py").write_text("import boto3\nboto3.client('eks').create_cluster(name='c')\n")
            payload = {**bash("python3 up.py"), "cwd": tmp}
            self.assertEqual(decision(run(payload, {})), "ask")

    def test_unrelated_mcp_tool_is_ignored(self):
        payload = mcp("mcp__github__create_issue", title="aws ec2 run-instances fails", body="aws eks create-cluster")
        self.assertIsNone(run(payload, {}))

    def test_non_aws_tool_is_ignored(self):
        self.assertIsNone(run({"tool_name": "Read", "tool_input": {"file_path": "/tmp/aws ec2"}}, {}))


class ConfigTest(unittest.TestCase):
    def test_user_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "costaware").mkdir()
            Path(tmp, "costaware", "config.json").write_text(json.dumps({"unknown_service_read": "LOW"}))
            response = run(bash("aws apigateway get-rest-apis"), {"XDG_CONFIG_HOME": tmp})
        self.assertNotIn("permissionDecision", response["hookSpecificOutput"])

    def test_project_config_can_only_tighten(self):
        # .costaware.json sits in the agent's workspace, so it can't loosen the gate.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".costaware.json").write_text(json.dumps({"unknown_service_read": "LOW", "high_monthly_usd": 50}))
            env = {"CLAUDE_PROJECT_DIR": tmp, "XDG_CONFIG_HOME": tmp}
            self.assertEqual(decision(run(bash("aws apigateway get-rest-apis"), env)), "ask")
            self.assertEqual(decision(run(bash("aws ec2 allocate-address"), env)), "ask")
            Path(tmp, ".costaware.json").write_text(json.dumps({"unknown_service_read": "HIGH"}))
            Path(tmp, "costaware").mkdir()
            Path(tmp, "costaware", "config.json").write_text(json.dumps({"unknown_service_read": "LOW"}))
            response = run(bash("aws apigateway get-rest-apis"), env)
        self.assertIn("[HIGH]", response["hookSpecificOutput"]["permissionDecisionReason"])

    def test_broken_config_fails_closed_for_aws_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text("{not json")
            env = {"COSTAWARE_CONFIG": str(path)}
            self.assertEqual(decision(run(bash("aws sts get-caller-identity"), env)), "ask")
            self.assertIsNone(run(bash("ls"), env))


if __name__ == "__main__":
    unittest.main()


def antigravity(name, workspace=None, user_input="do it", **args):
    return {"conversationId": "c1", "workspacePaths": [workspace] if workspace else [], "stepIdx": 3,
            "lastUserInput": user_input, "toolCall": {"name": name, "args": args}}


def approval_code(reason):
    return re.search(r"costaware approve ([0-9a-f]{6})", reason).group(1)


class AntigravityHookTest(unittest.TestCase):
    def test_free_allows(self):
        self.assertEqual(run(antigravity("run_command", CommandLine="aws sts get-caller-identity"), {}),
                         {"decision": "allow"})
        self.assertEqual(run(antigravity("view_file", AbsolutePath="/tmp/x"), {}), {"decision": "allow"})

    def test_low_allows_with_reason(self):
        response = run(antigravity("run_command", CommandLine="aws s3 ls s3://b"), {})
        self.assertEqual(response["decision"], "allow")
        self.assertIn("costaware: LOW", response["reason"])

    def test_high_is_denied_until_approved_in_chat(self):
        command = "aws ec2 create-nat-gateway --subnet-id s"
        response = run(antigravity("run_command", CommandLine=command), {})
        self.assertEqual(response["decision"], "deny")
        self.assertIn("ec2 create-nat-gateway · recurring cost", response["reason"])
        code = approval_code(response["reason"])
        approved = run(antigravity("run_command", CommandLine=command, user_input=f"ok, costaware approve {code}"), {})
        self.assertEqual(approved["decision"], "auto_approve")

    def test_approval_survives_whitespace_changes(self):
        command = "aws ec2 create-nat-gateway --subnet-id s"
        code = approval_code(run(antigravity("run_command", CommandLine=command), {})["reason"])
        retry = antigravity("run_command", CommandLine="  aws ec2  create-nat-gateway --subnet-id s ",
                            user_input=f"costaware approve {code}")
        self.assertEqual(run(retry, {})["decision"], "auto_approve")
        script = 'await call_boto3(service_name="eks", operation_name="CreateCluster")'
        mcp_call = antigravity("call_mcp_tool", ServerName="aws-mcp", ToolName="aws___run_script",
                               Arguments={"code": script})
        code = approval_code(run(mcp_call, {})["reason"])
        mcp_call["toolCall"]["args"]["Arguments"]["code"] = script + "   \n"
        mcp_call["lastUserInput"] = f"costaware approve {code}"
        self.assertEqual(run(mcp_call, {})["decision"], "auto_approve")

    def test_approval_is_tied_to_the_exact_call(self):
        command = "aws ec2 create-nat-gateway --subnet-id s"
        code = approval_code(run(antigravity("run_command", CommandLine=command), {})["reason"])
        other = run(antigravity("run_command", CommandLine=command + "-2", user_input=f"costaware approve {code}"), {})
        self.assertEqual(other["decision"], "deny")
        self.assertEqual(run(antigravity("run_command", CommandLine=command, user_input="yes approve it"), {})["decision"],
                         "deny")

    def test_script_resolved_against_cwd_arg(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "up.py").write_text("import boto3\nboto3.client('eks').create_cluster(name='c')\n")
            response = run(antigravity("run_command", CommandLine="python3 up.py", Cwd=tmp), {})
        self.assertEqual(response["decision"], "deny")

    def test_mcp_script_tool(self):
        # Antigravity routes MCP calls through call_mcp_tool (payload shape captured from agy 1.4.7).
        code = 'nat = await call_boto3(service_name="ec2", operation_name="CreateNatGateway", params={"SubnetId": "s"})'
        payload = antigravity("call_mcp_tool", ServerName="aws-mcp", ToolName="aws___run_script",
                              Arguments={"code": code})
        response = run(payload, {})
        self.assertEqual(response["decision"], "deny")
        self.assertIn("ec2 create-nat-gateway", response["reason"])
        payload["toolCall"]["args"]["Arguments"] = json.dumps({"code": code})
        self.assertEqual(run(payload, {})["decision"], "deny")
        payload["toolCall"]["args"]["Arguments"] = {"code": 'await call_boto3(service_name="ec2", operation_name="DescribeVpcs")'}
        self.assertEqual(run(payload, {}), {"decision": "allow"})

    def test_project_config_from_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".costaware.json").write_text(json.dumps({"high_monthly_usd": 0.0}))
            response = run(antigravity("run_command", workspace=tmp, CommandLine="aws secretsmanager create-secret --name s"), {})
        self.assertEqual(response["decision"], "deny")

    def test_approval_skips_the_prompt_only_for_plain_aws_commands(self):
        command = "aws ec2 create-nat-gateway --subnet-id s && rm -rf build"
        code = approval_code(run(antigravity("run_command", CommandLine=command), {})["reason"])
        approved = run(antigravity("run_command", CommandLine=command, user_input=f"costaware approve {code}"), {})
        self.assertEqual(approved["decision"], "allow")  # the user's own permission rules still apply to `rm`

    def test_broken_config_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text("{not json")
            env = {"COSTAWARE_CONFIG": str(path)}
            self.assertEqual(run(antigravity("run_command", CommandLine="aws s3 ls"), env)["decision"], "deny")
            self.assertEqual(run(antigravity("run_command", CommandLine="ls"), env), {"decision": "allow"})


class CodexHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"COSTAWARE_STATE_DIR": self.tmp.name}

    def tearDown(self):
        self.tmp.cleanup()

    def tool(self, command, session="s1"):
        return {"session_id": session, "turn_id": "t1", "hook_event_name": "PreToolUse", "cwd": "/tmp",
                "model": "gpt", "tool_name": "Bash", "tool_input": {"command": command}}

    def prompt(self, text, session="s1"):
        return {"session_id": session, "turn_id": "t2", "hook_event_name": "UserPromptSubmit", "prompt": text}

    def test_detected_from_payload(self):
        self.assertIsNone(run(self.tool("aws sts get-caller-identity"), self.env))
        response = run(self.tool("aws s3 ls"), self.env)["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", response)

    def test_high_is_denied_never_asked(self):
        output = run(self.tool("aws eks create-cluster --name c"), self.env)["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("costaware approve", output["permissionDecisionReason"])

    def test_chat_approval_via_user_prompt(self):
        command = "aws eks create-cluster --name c"
        code = approval_code(run(self.tool(command), self.env)["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(run(self.prompt(f"costaware approve {code}"), self.env))
        output = run(self.tool(command), self.env)["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", output)
        # Approvals belong to the session and last until the user's next prompt.
        self.assertIn("permissionDecision", run(self.tool(command, session="s2"), self.env)["hookSpecificOutput"])
        run(self.prompt("thanks, now something else"), self.env)
        self.assertEqual(run(self.tool(command), self.env)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_tool_output_cannot_approve(self):
        # Only a UserPromptSubmit event records approvals; text in a command doesn't.
        command = "aws eks create-cluster --name c"
        code = approval_code(run(self.tool(command), self.env)["hookSpecificOutput"]["permissionDecisionReason"])
        run(self.tool(f"echo costaware approve {code}"), self.env)
        self.assertEqual(run(self.tool(command), self.env)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_agent_cannot_write_approvals(self):
        for command in [f"echo costaware approve abc123 > {self.tmp.name}/s1.txt",
                        "cp a.txt ~/.cache/costaware/approvals/s1.txt"]:
            output = run(self.tool(command), self.env)["hookSpecificOutput"]
            self.assertEqual(output["permissionDecision"], "deny")
            self.assertIn("Only the user can approve", output["permissionDecisionReason"])
