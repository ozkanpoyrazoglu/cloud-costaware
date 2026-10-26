# Cloud Costaware for AWS

A pre-execution cost gate for AI coding agents working on AWS. It classifies the AWS calls an agent is about to make by cost, before they run:

| Level | Meaning | What happens |
|---|---|---|
| `FREE` | No charge | The call runs |
| `LOW` | Small per-request fee, or a resource that costs ~$0 while idle | The call runs; the agent mentions the cost |
| `HIGH` | Costs money while idle (above the threshold), or a large one-time fee | The call waits for the user's approval |
| `UNKNOWN` | Size-dependent (Athena, DynamoDB scan, Logs Insights...) or no rule | The call waits for the user's approval |

The product is Cloud Costaware; the package, CLI command, plugin and skill are all named `costaware`. It ships as a Python package with a CLI, plus a `PreToolUse` hook and a skill for three coding agents: **Claude Code**, **Antigravity CLI** (the successor of Gemini CLI) and **Codex CLI**. It works alongside the AWS MCP server you already use; there is no extra MCP server to run. See [RESEARCH.md](RESEARCH.md) for the background.

No dependencies: Python 3.9+ and the standard library.

## What the hook reads

- **Shell commands**: `aws ...` invocations, and the boto3 calls in Python run from the shell (`python -c '...'`, `python script.py` resolved against the session's working directory, and `python3 - <<EOF` heredocs).
- **MCP tool calls**, in three shapes:
  - Script tools that take boto3 Python in `code`, `script` or `python_code`. This includes `aws___run_script` from the managed [AWS MCP Server](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/understanding-mcp-server-tools.html), AWS's recommended MCP server.
  - CLI-style tools that take an `aws ...` string (or a list of them) in `cli_command`, `command`, `cmd` or `aws_command`. This includes `call_aws` from the older, now superseded [AWS API MCP Server](https://github.com/awslabs/mcp/tree/main/src/aws-api-mcp-server).
  - API-style tools that take `service` + `operation` (+ `parameters`/`params`).

  Other MCP tools pass through untouched. Antigravity routes every MCP call through its `call_mcp_tool` wrapper (`ServerName`, `ToolName`, `Arguments`); the hook unwraps it.

If costaware itself fails, for example on a broken config file, calls that mention `aws` are held for approval rather than slipping through.

## How approval works

| Agent | HIGH / UNKNOWN | Why |
|---|---|---|
| Claude Code | The hook returns `ask`: the normal permission prompt, showing the estimate. It also overrides allow rules such as `Bash(aws:*)` and `bypassPermissions` mode. | Verified live. In non-interactive (`-p`) runs the call is denied. |
| Antigravity CLI | Chat approval (below). Once approved, a plain `aws` command or an MCP call gets `auto_approve`, so there's no second prompt; anything else (pipes, other programs, scripts) gets `allow`, so the user's own permission rules still apply. | Antigravity's `ask`/`force_ask` decisions are skipped under `--dangerously-skip-permissions` (verified live), and a hook can't tell which mode it runs in. |
| Codex CLI | Chat approval (below). | Codex hooks can't ask: an `ask` is treated as a hook failure and the call runs. |

**Chat approval:**

1. The call is denied before it runs. The agent receives the estimate and a 6-character code derived from that exact call.
2. The agent explains the cost and asks the user to reply `costaware approve <code>`.
3. The agent retries the identical call. It runs only if the user's own latest message contains the code. In Antigravity the hook reads it from the payload's `lastUserInput`; in Codex, a `UserPromptSubmit` hook records it.

The model can't write user messages, so it can't approve its own calls. A changed call gets a new code, and approvals expire with the user's next message.

## Install

### Claude Code

```
/plugin marketplace add ozkanpoyrazoglu/cloud-costaware
/plugin install costaware@cloud-costaware
```

Or, from a local checkout: `claude --plugin-dir /path/to/cloud-costaware`. The plugin bundles the hook (`hooks/hooks.json`) and the skill.

Without the plugin, install the package and add this to `settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash|mcp__.*", "hooks": [{"type": "command", "command": "costaware hook"}]}
    ]
  }
}
```

### Antigravity CLI

```
agy plugin install https://github.com/ozkanpoyrazoglu/cloud-costaware
# or from a local checkout:
agy plugin install /path/to/cloud-costaware
```

The repo root is also an Antigravity plugin: `plugin.json` and `hooks.json` at the root, skills in `skills/`. Hook commands run from the plugin directory, so the bundled `hooks/pretooluse.py` needs no install.

### Codex CLI

Add the hook to `~/.codex/hooks.json` (or `<project>/.codex/hooks.json` for a trusted project). Both events are needed: `UserPromptSubmit` records approvals.

```json
{
  "hooks": {
    "PreToolUse": [{"hooks": [{"type": "command", "command": "python3 /path/to/costaware/hooks/pretooluse.py --client codex"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 /path/to/costaware/hooks/pretooluse.py --client codex"}]}]
  }
}
```

With the package installed, `costaware hook --client codex` works too. Codex runs new or changed hooks only after you trust them in `/hooks`. Approvals are kept per session in `~/.cache/costaware/approvals/` (override with `COSTAWARE_STATE_DIR`).

The skill ([`skills/costaware/SKILL.md`](skills/costaware/SKILL.md)) tells the agent how to explain costs before a HIGH call: hourly and monthly figures, the hidden costs that come with a resource, and how to undo it. It also covers sizing UNKNOWN calls (Athena, DynamoDB scans, S3 downloads) with free read-only calls first.

### How Python is analyzed

Python is read statically with Python's `ast` module and is never executed. It recognizes two styles:

- **The AWS MCP Server sandbox** (`aws___run_script`), which has no boto3 clients and calls AWS through `await call_boto3(service_name="ec2", operation_name="CreateNatGateway", region_name=..., params={...})`. Literal service and operation names are classified, and `params` drives argument-aware rules. Calls to the sandbox's higher-level `aws_mcp.*` helpers are UNKNOWN, apart from `aws_mcp.search_functions`, because the API calls they make aren't visible.
- **boto3 clients**, in scripts run from the shell.

For boto3 clients, the analyzer:

- tracks clients created with `boto3.client("svc")` or `session.client(...)`, including clients stored on `self` and clients used inside functions defined before the assignment;
- maps snake_case operations to the rule table (`create_nat_gateway` → `ec2 create-nat-gateway`), and maps S3 transfer helpers (`upload_file`, `download_file`) to the API call they make;
- uses literal keyword arguments for argument-aware rules (`DryRun=True`, `BillingMode="PAY_PER_REQUEST"`). When an argument is only known at runtime and could raise the cost, it assumes the costliest case.

The following are UNKNOWN:
- calls whose service or operation is computed at runtime (`boto3.client(name)`, `getattr(client, op)`, `call_boto3(service_name=svc, ...)`);
- the `boto3.resource` API, apart from reads and object constructors such as `s3.Bucket("b")`;
- in AWS tools or code that imports boto3, costly-looking calls (`create_*`, `run_*`, `put_*`...) on objects that can't be resolved to a client.

**Known gaps:**
- Terraform, CDK, CloudFormation templates applied by other tools, and SDKs in other languages are not analyzed.
- Python modules imported by a script are not followed.
- Scripts that can't be read (missing, larger than 1MB) are skipped.

**Threat model.** costaware guards against an agent that runs a costly call by mistake or without saying what it costs. It is not a sandbox against an agent that sets out to evade it: obfuscated code (`exec`, generated scripts, downloaded tools) can get past any static check. Pair it with least-privilege IAM and AWS Budgets. Shell patterns an agent writes in normal work (redirects such as `2>&1`, subshells, `if ... then`, wrappers, heredocs) are treated as bugs when they hide a call; tricks only a deliberate evader would build (Python/shell polyglots, `exec` of generated strings) are out of scope. Within that scope, the gate fails closed: a command it can't parse is UNKNOWN when it mentions `aws`, an `aws` call behind an unknown wrapper (`timeout 60 aws ...`) is still found, and commands that touch the approval phrase or costaware's approval state are HIGH.

In Codex, approvals are kept in `~/.cache/costaware/approvals`, which Codex's default `workspace-write` sandbox can't write to (verified). That sandbox is what keeps the agent from approving its own calls; the text check above is only a tripwire. Under `danger-full-access`, or with `COSTAWARE_STATE_DIR` pointing into the workspace or `/tmp`, the agent could write approvals itself.

## CLI

```bash
# Without installing
PYTHONPATH=src python3 -m costaware classify 'aws ec2 create-nat-gateway --subnet-id s-1'

# Installed
uv tool install .   # or: pip install .
costaware classify 'aws s3 sync s3://bucket/data ./data && aws sts get-caller-identity'
```

```
Result: UNKNOWN
  UNKNOWN aws s3 sync s3://bucket/data ./data
          s3 sync · size-dependent, no estimate
          Data transfer to the internet ~$0.09/GB (first 100GB/month free) + GET requests. ...
  FREE    aws sts get-caller-identity
          sts get-caller-identity · free
```

The argument can be a whole shell line. These are unwrapped: `&&`, `|`, `;`, newlines, `$(...)`, `bash -c '...'`, `AWS_PROFILE=x`, and `sudo`/`xargs` prefixes. When there are several calls, the most severe level is the result.

For API-style calls (e.g. from MCP tools):

```bash
costaware classify --service dynamodb --operation CreateTable --param BillingMode=PAY_PER_REQUEST
costaware classify --service ec2 --operation RunInstances --param DryRun=true --format json
```

For Python code:

```bash
costaware classify --python plan.py
echo 'import boto3; boto3.client("eks").create_cluster(name="c")' | costaware classify --python -
```

`--format json` gives machine-readable output. Pass `-` as the command to read it from stdin. `costaware hook [--client claude|antigravity|codex]` reads a hook payload from stdin; the client is detected from the payload when omitted.

## Rule table

The rules live in [`src/costaware/rules/aws.json`](src/costaware/rules/aws.json) and cover 32 services. Each rule carries a `cost` type, and the level is derived from it:

| `cost` | Level |
|---|---|
| `free` | FREE |
| `request`, `usage` | LOW |
| `recurring` | LOW if `monthly_usd` is below the threshold, HIGH if at/above or unknown |
| `one_time` | LOW if `usd` is below the threshold, HIGH if at/above or unknown |
| `variable` | UNKNOWN (will become a number once estimators exist) |

Rules can look at arguments:
- `--dry-run` makes any EC2 call FREE.
- `dynamodb create-table --billing-mode PAY_PER_REQUEST` is LOW, while the default (PROVISIONED) is HIGH.
- `s3 cp/sync` tells downloads from uploads.

Match order:
1. service rules
2. `global_rules` (delete/stop, tagging)
3. the service's `default_read`/`default_write`
4. UNKNOWN

Prices are approximate us-east-1 list prices. Rules with `confidence: "low"` need verification.

## Configuration

The CLI takes `--config settings.json`. The hook reads the user's config from `COSTAWARE_CONFIG`, or `~/.config/costaware/config.json` (`$XDG_CONFIG_HOME` is honoured):

```json
{
  "high_monthly_usd": 1.0,
  "high_one_time_usd": 1.0,
  "unknown_service_read": "UNKNOWN"
}
```

- `high_monthly_usd`: idle monthly cost at or above this is HIGH. With the default of $1:
  - HIGH: KMS key ($1), Elastic IP (~$3.65), dashboard ($3)
  - LOW: alarm ($0.10), secret ($0.40)
- `high_one_time_usd`: one-time cost at or above this is HIGH.
- `unknown_service_read`: level for `describe-`/`list-`/`get-` calls of services missing from the rule table. The safe default is `UNKNOWN`. Set it to `LOW` if it asks too often.

A `.costaware.json` in the project root is applied on top, but only where it makes the gate stricter (lower thresholds, a stricter `unknown_service_read`). The agent can write to its workspace, so a project file must not be able to switch the gate off; loosening it takes the user-level file.

The hook also honours `COSTAWARE_RULES` to point at an alternative rule table.

## Layout

```
src/costaware/          package: shell parser, Python analyzer (pyscan), classifier, report, CLI, hook
src/costaware/rules/    rule table
.claude-plugin/         Claude Code plugin and marketplace manifests
hooks/                  Claude Code hook config (hooks.json) and the shared hook entry point
plugin.json, hooks.json Antigravity plugin manifest and hook config
skills/costaware/       skill (SKILL.md + classify script)
tests/
```

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## License

[Apache-2.0](LICENSE)
