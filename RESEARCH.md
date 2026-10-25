# Cost-Aware AWS Agent Skill — Research Notes

_Date: 2026-10-13_

## Idea

When Claude, Gemini or ChatGPT agents act on AWS (via MCP or otherwise):

- For **costly** operations (creating resources, Athena queries, etc.) they should take cost into account and tell the user.
- For **free** operations they should carry on without stopping.

## What exists today

No skill was found that does exactly this, i.e. nothing **stops** a costly operation pending approval. The pieces exist:

| Tool | What it does | What's missing |
|---|---|---|
| `aws-cost-operations` skill (zxkane/aws-skills, also copied into aws-samples) | Tells the agent to "estimate cost before creating resources", uses the AWS Pricing MCP | Instructions only. Nothing enforces it if the model skips it. Includes Claude hooks; review before installing. |
| `awslabs/aws-pricing-mcp-server` | List prices and pre-deployment estimates. Replaces the old `cost-analysis-mcp-server`. | Doesn't track actual spend. Needs AWS credentials. |
| Billing / Cost Management MCP | Cost Explorer, Budgets, Anomalies (actual spend) | Looks backwards, doesn't block operations up front. Maintainer not verified. |

**Conclusion:** The information sources are there. What's missing is a **decision layer** that classifies an operation before it runs and stops it when needed.

## Why it's worth building

1. **A skill alone is only advice.** The real value is enforcement. In Claude Code a `PreToolUse` hook can do that: it catches `aws ...` commands or MCP calls before they run.
2. **It has to work with several models.** Gemini and ChatGPT don't use Claude skills, but they all speak MCP. If the core logic is a **cost-gate MCP server** (or a proxy in front of AWS MCPs), all three agents go through the same check.
3. **"It's only reading, so it's free" is wrong.** The value is in these details:
   - Every Cost Explorer API request is billed (about $0.01).
   - CloudWatch `GetMetricData` is billed.
   - S3 `LIST`/`GET` requests have a small fee. _(Correction 2026-10-19: listing doesn't add up as much as assumed. One LIST request returns 1,000 objects; at $0.005/1,000 requests, 1 million objects costs ~$0.005. The real risk is downloading: data transfer to the internet is ~$0.09/GB.)_
   - Most `Describe*` and `List*` calls, and IAM and STS, really are free.
4. **Athena is a special case.** There's no dry run like BigQuery's. It's billed per TB scanned (about $5/TB), with a 10 MB minimum per query.
   - Estimation approach:
     1. Find the table and partition locations in the Glue Catalog.
     2. Sum the S3 sizes of the partitions matching the query's `WHERE` clause.
     3. For Parquet/ORC, scale down by the selected columns.
   - Don't use `EXPLAIN ANALYZE`; it actually runs the query.
   - This estimator would be useful on its own.

## Proposed architecture

```
Agent (Claude / Gemini / GPT)
        │
        ▼
 cost-gate MCP  ──► classifier: FREE | LOW | HIGH | UNKNOWN
        │              ├─ Pricing MCP / Price List API (unit prices)
        │              ├─ Athena scan estimator (Glue + S3 sizes)
        │              └─ rule table (API → cost type)
        ▼
 Decision:
   FREE     → carry on
   LOW      → report "~$0.03 estimated", carry on
   HIGH     → stop, ask the user
   UNKNOWN  → stop, ask (safe default)
```

Two thin layers on top:

- **SKILL.md** (for Claude): tells the model when and how to report. For example, showing hourly and monthly cost separately, and reminding about hidden costs such as NAT Gateways and EIPs.
- **Claude Code hook**: a second safety net in case the MCP is bypassed and the `aws` CLI is called directly.

## Risks

- Price estimates depend on usage. For services like Lambda, S3 and data transfer, estimates should be given as ranges; a single number is misleading.
- Creating a resource means ongoing cost from that moment on. Reports should say "~$Y/month while it runs", not "$X now".
- Maintaining the rule table will take effort. The first version should focus on the 15-20 most used services.

## Next steps

- [x] Pick the starting path: _(2026-10-19)_ Both, in order. The core is a separate Python package (`costaware`, with a CLI). For Claude, a plugin (skill + hook + MCP); for Gemini/GPT, an MCP server on the same core.
- [x] Build the free/paid AWS API classification table. _(`src/costaware/rules/aws.json`, 32 services)_
- [x] Set the LOW/HIGH thresholds. _(Initial value: idle monthly ≥ $1 or one-time ≥ $1 → HIGH. Configurable via `--config` / `.costaware.json`.)_
- [x] Claude Code plugin: `PreToolUse` hook (Bash + AWS MCP tools) and `SKILL.md`. _(2026-10-25. Verified live: the hook's `ask` overrides a `Bash(aws:*)` allow rule; LOW context reaches the model.)_
- [ ] ~~Athena scan estimator prototype.~~ _(2026-10-25: dropped. Estimating means Glue and S3 listing calls before every query, which costs money and makes each response slower. Athena queries stay UNKNOWN and need approval; the skill tells the agent how to size a query manually when it matters.)_
- [ ] ~~MCP layer (for Gemini/GPT).~~ _(2026-10-25: dropped as the default. Users already run an AWS MCP server, and a second server next to it is easy for the model to bypass. Gemini CLI and Codex CLI have their own hooks, so the gate is ported there instead. A separate MCP server is only worth it for clients without hooks, such as ChatGPT on the web.)_
- [x] Detect boto3 calls in Python. _(2026-10-25. Static `ast` analysis, used for `python` run from Bash and for the AWS MCP Server's `aws___run_script`.)_
- [x] Antigravity CLI and Codex CLI hook adapters. _(2026-10-25. Gemini CLI was replaced by Antigravity CLI (`agy`) on 2026-06-18 for consumer users. Antigravity hooks have `ask`/`force_ask`, but they are skipped under `--dangerously-skip-permissions` (verified live), so HIGH/UNKNOWN calls use chat approval: deny + a per-call code that only counts in the user's own message (`lastUserInput`). Verified live end to end, also as an installed plugin. Codex hooks can't ask at all (`ask` = hook failure, the call runs), so it uses the same chat approval, with codes recorded by a `UserPromptSubmit` hook. Verified live in Codex too, over a resumed session. The first approval attempt failed because the model re-typed the command between separate runs, so codes are now computed on whitespace-normalized calls. Codex shows these hooks as "Unnamed hook" in `/hooks` and runs them only after they are trusted there.)_
- [x] Check whether Claude Code's hook `ask` survives `bypassPermissions`. _(2026-10-25: it does. Under `--permission-mode bypassPermissions` the `ask` still became a permission request, which `-p` mode denied; Antigravity in the same situation ran the call. Claude Code keeps the native prompt.)_
- [ ] Terraform/CDK and SDKs in other languages.

## Update 2026-10-25: the AWS MCP landscape

- AWS now recommends the managed **AWS MCP Server** (`https://aws-mcp.<region>.api.aws/mcp`, via OAuth or `mcp-proxy-for-aws`). The older `aws-api-mcp-server` (with `call_aws`) is superseded.
- The managed server executes AWS calls through **`aws___run_script(code=...)`**: Python with AWS API access, run in a sandbox. Gating it means reading boto3 code, not CLI strings.
- `aws-api-mcp-server` has its own security policy (`~/.aws/aws-api-mcp/mcp-security-policy.json`, with `denyList` and `elicitList` on exact `aws <service> <operation>` strings) and `REQUIRE_MUTATION_CONSENT`. Both depend on the client supporting elicitation. The policy layer had a fail-open bug (CVE-2026-16584, fixed in 1.3.47).
- The sandbox API, verified from the server's `tools/list`: there are no boto3 clients. Scripts call `await call_boto3(*, service_name, operation_name, region_name=None, params=None)` with PascalCase operation names, and end with `result` as the final expression. A pre-imported `aws_mcp` module offers higher-level helpers (`help(aws_mcp)`, `aws_mcp.search_functions(...)`). The server's instructions tell agents not to use the AWS CLI from Bash. The tool is annotated `destructiveHint: true`.
- Antigravity doesn't expose MCP tools under their own names. Every MCP call goes through a `call_mcp_tool` wrapper with `{ServerName, ToolName, Arguments}`, found with a blocking probe. The first live run let a `CreateNatGateway` script through because the hook looked for `code` at the top level; it was harmless only because the subnet ID was fake. Fixed, and verified live, in both Antigravity and Claude Code with the AWS MCP Server attached.
- The analyzer understands `call_boto3`, and treats `aws_mcp.*` helpers as UNKNOWN. Before this check it only knew boto3 clients, so every sandbox call passed as FREE.

## Sources

- [aws-cost-operations skill (skills.sh)](https://www.skills.sh/zxkane/aws-skills/aws-cost-operations)
- [aws-cost-operations (Smithery)](https://smithery.ai/skills/zxkane/aws-cost-operations)
- [AWS Pricing MCP Server announcement (AWS Blog)](https://aws.amazon.com/blogs/aws-cloud-financial-management/aws-price-list-gets-a-natural-language-upgrade-introducing-the-aws-pricing-mcp-server)
- [awslabs.cost-analysis-mcp-server (PyPI, deprecated)](https://pypi.org/project/awslabs.cost-analysis-mcp-server/)
- [billing-cost-management-mcp-server (Docker Hub)](https://hub.docker.com/r/mcp/billing-cost-management-mcp-server)
- [AWS API MCP Server (awslabs/mcp)](https://github.com/awslabs/mcp/tree/main/src/aws-api-mcp-server)
- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks.md)
- [Understanding the AWS MCP Server tools](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/understanding-mcp-server-tools.md)
- [MCP Proxy for AWS](https://github.com/aws/mcp-proxy-for-aws)
- [Gemini CLI hooks reference](https://geminicli.com/docs/hooks/reference.md)
- [Codex hooks](https://learn.chatgpt.com/docs/hooks)
