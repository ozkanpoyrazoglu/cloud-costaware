---
name: costaware
description: Cost-aware AWS operations. Use whenever you are about to run AWS CLI commands or AWS MCP tools that create, start, modify, scale, query or transfer anything (EC2, RDS, NAT Gateway, EKS, Athena, DynamoDB scans, S3 downloads, CloudFormation, SageMaker...), when planning AWS infrastructure, or when a "costaware:" message appears in a tool result or permission prompt.
---

# Cost-aware AWS operations

A `PreToolUse` hook classifies AWS calls before they run: `aws ...` shell commands, boto3 calls in Python you run from the shell (`python -c`, `python script.py`, heredocs), and AWS MCP calls (including the boto3 code sent to the AWS MCP Server's `aws___run_script`):

| Level | Meaning | What happens | What you do |
|---|---|---|---|
| `FREE` | No charge | Nothing, the call runs | Carry on, don't mention cost |
| `LOW` | Per-request or pay-per-use fee, or a resource whose looked-up idle cost is below the threshold | Call runs; a `costaware: LOW` note is added to your context | Mention the cost in one short line, carry on |
| `HIGH` | Costs money while idle (at or above the threshold, or no price was found), or a one-time fee | The call waits for the user's approval | Explain the cost **before** the call (see below) |
| `UNKNOWN` | Size-dependent (Athena, DynamoDB scan, Logs Insights, S3 download...) or no rule | The call waits for the user's approval | Estimate the size first if you can, then explain |

The hook is the enforcement. Your job is to make the user's decision an informed one and to avoid surprising them.

costaware holds no prices. When it can, the hook looks the on-demand list price up in the AWS Price List API and adds a `Price List (...)` line to its message. Quote that line; when it is missing, look the price up yourself (below). Never quote prices from memory: they differ by region and change over time.

## When a call is stopped with an approval code

Depending on the agent, approval is either a permission prompt or happens in chat. In chat, the call fails with a `costaware: HIGH` (or `UNKNOWN`) message that ends with `costaware approve <code>`. Then:

1. Explain the cost to the user as described below.
2. Ask them to reply with exactly `costaware approve <code>`. Give the code verbatim.
3. After they reply with it, retry the **identical** call. Any change to the command or script produces a different code.

Never type the approval phrase yourself, for example in a command or a file. It only counts when it comes from the user's own message. Don't rewrite the call to dodge the check. If the user wants a cheaper variant instead, run that one; it gets its own classification.

If the message has no approval code (Claude Code), approval is the permission prompt itself. In a non-interactive run nobody can answer it, so the call is denied. Report the cost and stop; don't suggest raising thresholds or editing costaware's config to push a single call through.

## Before a HIGH or UNKNOWN call

Say what it will cost before the call triggers the prompt, so the confirmation dialog is not the first time the user hears about it. Keep it short:

```
This creates a NAT Gateway in eu-central-1 (Price List, on-demand):
  $<hourly>/hour → ~$<monthly>/month while it exists, plus $<per-GB>/GB processed.
  It keeps billing until deleted: aws ec2 delete-nat-gateway --nat-gateway-id <id>
```

Rules for the estimate:

- **Recurring resources: give hourly and monthly** (`$<hourly>/hour → ~$<monthly>/month`). Say "while it exists", not "now". Monthly = hourly × 730.
- **Usage-based costs: give a range or a per-unit price**, not a single number (`$<per-TB>/TB scanned; this table is ~40GB → ~$<estimate>`).
- **Name the hidden costs** that come with the resource, and price them the same way:
  - EC2: EBS volumes (billed even when stopped), a public IPv4 address each, snapshots.
  - Private subnets: a NAT Gateway, often one per AZ.
  - RDS: Multi-AZ doubles the price; storage and backups are billed separately.
  - EKS: the control plane is billed hourly before any node.
  - Interface VPC endpoints: billed per AZ.
  - CloudWatch Logs: retention defaults to forever.
- **Region matters.** Price the region the call targets, not us-east-1.
- **Say how to undo it** (the delete/stop command) when the resource keeps billing.
- If several costly resources are planned (e.g. "set up a VPC with an ALB and RDS"), give one total up front instead of surprising the user call by call.

### Looking up the price

The hook prices most resources itself (EC2 and RDS instances, EBS, NAT Gateway, Elastic IP, VPC endpoints, load balancers, EKS, KMS, Secrets Manager, CloudWatch, Athena, S3, DynamoDB, Lambda...) and adds a `Price List (...)` line; your figure should agree with it. You still say the cost first, since the line is left out when the lookup fails.

For anything else (another instance or node type, ElastiCache, OpenSearch, Redshift, SageMaker, a missing line), get the price from the Price List API. `pricing get-products` is free and the gate lets it through. The API is served from `us-east-1` (also `eu-central-1`, `ap-south-1`); `regionCode` selects the region being priced. Usage types carry a region prefix (`EUC1-NatGateway-Hours`), so match them with `Type=CONTAINS`.

```bash
# EC2 on-demand Linux, per hour. Without tenancy/preInstalledSw/capacitystatus/licenseModel you get many products.
aws pricing get-products --region us-east-1 --service-code AmazonEC2 --filters \
  Type=TERM_MATCH,Field=instanceType,Value=m5.2xlarge Type=TERM_MATCH,Field=regionCode,Value=eu-central-1 \
  Type=TERM_MATCH,Field=operatingSystem,Value=Linux Type=TERM_MATCH,Field=tenancy,Value=Shared \
  Type=TERM_MATCH,Field=preInstalledSw,Value=NA Type=TERM_MATCH,Field=capacitystatus,Value=Used \
  Type=TERM_MATCH,Field=licenseModel,Value="No License required"

# EBS, per GB-month
aws pricing get-products --region us-east-1 --service-code AmazonEC2 --filters \
  Type=TERM_MATCH,Field=regionCode,Value=eu-central-1 Type=TERM_MATCH,Field=productFamily,Value=Storage \
  Type=TERM_MATCH,Field=volumeApiName,Value=gp3

# RDS, per hour (deploymentOption: Single-AZ | Multi-AZ)
aws pricing get-products --region us-east-1 --service-code AmazonRDS --filters \
  Type=TERM_MATCH,Field=instanceType,Value=db.r6g.large Type=TERM_MATCH,Field=regionCode,Value=eu-central-1 \
  Type=TERM_MATCH,Field=databaseEngine,Value=PostgreSQL Type=TERM_MATCH,Field=deploymentOption,Value=Multi-AZ
```

With the AWS MCP Server, make the same call in `aws___run_script`: `call_boto3(service_name="pricing", operation_name="GetProducts", region_name="us-east-1", params={"ServiceCode": ..., "Filters": [{"Type": "TERM_MATCH", "Field": ..., "Value": ...}]})`.

Each `PriceList` entry is a JSON string; the price is in `terms.OnDemand.*.priceDimensions.*.pricePerUnit.USD`. If a filter matches several products, add filters rather than picking one. For other services, `pricing describe-services --service-code <code>` lists the attribute names and `pricing get-attribute-values` their values. Quote the result as an on-demand list price: it doesn't include Spot, Reserved Instances or Savings Plans, and the AMI's OS (Windows, RHEL) can change the EC2 price.

When the user declines, don't retry the same call or route around the gate (e.g. switching from `aws` CLI to an SDK script or another MCP tool, or hiding the call behind `getattr`). Offer a cheaper alternative if one exists:
- DynamoDB `PAY_PER_REQUEST` instead of provisioned
- a smaller instance type, or a spot instance
- a Gateway endpoint instead of an Interface endpoint for S3/DynamoDB
- `--dry-run` to check permissions only

## UNKNOWN: estimate the size first

These calls are billed by data volume. Free, read-only calls usually give the size:

| Call | How to size it (free calls) | Billed by |
|---|---|---|
| `athena start-query-execution` | `glue get-table` / `get-partitions` for the S3 location, then `aws s3 ls --summarize --recursive` on the partitions the `WHERE` clause hits. Columnar formats (Parquet/ORC) only read selected columns. **Never use `EXPLAIN ANALYZE`, it runs the query.** | TB scanned, 10MB minimum per query |
| `dynamodb scan` | `dynamodb describe-table` → `TableSizeBytes` | read request units (4KB each, 8KB eventually consistent) |
| `logs start-query` | `logs describe-log-groups` → `storedBytes` (upper bound) | GB scanned |
| `s3 cp/sync` download | `aws s3 ls --summarize --recursive s3://...` | GB to the internet; free to EC2 in the same region |
| `cloudformation deploy` | Read the template and list the resources that cost money | sum of resources |

Multiply the size by the unit price in the `Price List (...)` line.

Note that `s3 ls --recursive` is itself a LOW call (a LIST request per 1,000 objects); that is fine for sizing.

## Checking a planned command without running it

The script is `scripts/classify.py` in this skill's folder (`${CLAUDE_SKILL_DIR}` in Claude Code):

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/classify.py" 'aws rds create-db-instance --db-instance-class db.t3.micro ...'
python3 "${CLAUDE_SKILL_DIR}/scripts/classify.py" --service dynamodb --operation CreateTable --param BillingMode=PROVISIONED
python3 "${CLAUDE_SKILL_DIR}/scripts/classify.py" --python plan.py   # boto3 code; '-' reads stdin
```

If the `costaware` package is installed, `costaware classify '...'` does the same. Use this when planning several steps, so you can quote a total before starting.

## Writing AWS code that the gate can read

The gate reads Python statically. Write AWS code so the costly calls are visible.

In the AWS MCP Server's `aws___run_script`, call `call_boto3` with literal names, and pass cost-relevant parameters inline:

```python
await call_boto3(service_name="dynamodb", operation_name="CreateTable",
                 params={"TableName": "orders", "BillingMode": "PAY_PER_REQUEST", ...})
```

Prefer `call_boto3` over the `aws_mcp.*` helpers for anything that creates resources. The gate can't see inside the helpers, so every helper call needs approval.

In boto3 scripts:

- Create clients with a literal service name: `ec2 = boto3.client("ec2")`, not `boto3.client(name)`.
- Call operations directly (`ec2.create_nat_gateway(...)`), not via `getattr` or a dict of clients.
- Pass cost-relevant arguments as literals (`BillingMode="PAY_PER_REQUEST"`, `VpcEndpointType="Gateway"`). If they are variables, the gate assumes the costliest case.
- Prefer the client API over the `boto3.resource` API, which the gate does not analyze and treats as UNKNOWN.
- Keep read-only exploration and resource creation in separate scripts, so a harmless lookup doesn't trigger a confirmation.

Anything the gate can't resolve is UNKNOWN, which means the user gets asked. That is safe but noisy.

## Things that look free but aren't

- Cost Explorer API (`aws ce ...`): every request is billed, every page.
- CloudWatch `get-metric-data`: billed per metric requested, no free tier.
- SQS: every API call, including `receive-message` polling, is billed.
- Deleting from S3 Glacier/IA early incurs a minimum-duration charge.

Most `describe-*`/`list-*`/`get-*` calls, and all IAM and STS calls, really are free. Don't add cost chatter to those.

## Configuration

Thresholds live in the user's `~/.config/costaware/config.json` (or a file named by `COSTAWARE_CONFIG`). A `.costaware.json` at the project root can only make them stricter:

```json
{"high_monthly_usd": 1.0, "unknown_service_read": "UNKNOWN"}
```

Thresholds are the user's standing policy, not a way to get one call through. Never edit these files yourself. Only if the user says the prompts are too frequent in general, suggest that they raise `high_monthly_usd` or set `unknown_service_read` to `"LOW"` in their user-level file, rather than disabling the hook.
