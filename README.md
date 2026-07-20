# ResolveOps

ResolveOps is an API-first enterprise Agent for handling long-tail business exceptions.

It is not an ERP chatbot and it is not a generic workflow demo. The core problem it targets is:

```text
An enterprise system has already detected an exception.
The normal deterministic workflow cannot safely decide the next step.
ResolveOps investigates, proposes an evidence-grounded plan, requests approval when needed,
executes controlled actions, and verifies the final business state.
```

Current reference integration: ERPNext.

ERPNext is used as a real open-source ERP sandbox and system of record. The Agent architecture is intentionally not ERPNext-specific: read tools, action schemas, policy checks, approval binding, executors and verifiers can be adapted to SAP, WMS, CRM, ticketing systems, procurement systems, or internal platforms.

## Why this project exists

Most enterprise processes should stay as normal workflows:

```text
order paid -> reserve stock -> create delivery note -> notify warehouse
```

ResolveOps only enters the exception branch:

```text
order cannot be fulfilled
-> why?
-> which systems must be checked?
-> what actions are possible?
-> which action is safe?
-> does it require approval?
-> did the external system actually change?
```

That boundary is important. The project demonstrates how to build an Agent that can work with real business systems without giving the model uncontrolled write access.

## What it demonstrates

- Case-based durable execution, not one-off chat completion.
- Read-only business tools exposed by schema and case type.
- Dynamic tool profile routing instead of exposing every tool to the LLM.
- Evidence-grounded planning with deterministic validation before approval.
- Write actions represented as controlled action plans, not directly callable LLM tools.
- Policy-controlled execution with role-based approval.
- Approval binding to case, plan version and action hash.
- Approval expiration, revocation and one-time consumption.
- Idempotent write execution.
- Read-after-write verification against ERPNext.
- Replanning when business state changes before execution.
- Manual review when the Agent cannot safely proceed.
- PostgreSQL-backed Case, Event, Task, Approval and Invocation state.
- Context isolation across multiple Cases.
- Lightweight verified lessons from successful Cases.
- Fault injection against ERPNext sandbox data.
- CLI-first operator/developer experience.
- Runtime status and execution evaluation APIs.

## Supported exception types

| Case type | What the Agent investigates | Safe outcome |
|---|---|---|
| `inventory_shortage` | Sales order, target/source inventory, transfer lanes, inbound purchase, customer delivery constraints | Transfer draft, purchase request, customer notification draft, or manual review |
| `price_mismatch` | Sales order item price, reference price, customer context | Price review record or manual review |
| `delivery_delay` | Sales order delivery date, inbound purchase schedule, supplier information, customer context | Supplier follow-up task or manual review |

The first case type is the most complete because it exercises the full loop: tool investigation, planning, approval, ERPNext write, verification, fault injection and replanning.

## Core flow

```text
Business exception
  -> Case
  -> Context Builder
  -> Tool Profile Router
  -> Read Tool Scheduler
  -> LLM Planner
  -> Evidence Grounding
  -> Policy Engine
  -> Bound Approval
  -> Governed Executor
  -> Read-after-write Verifier
  -> resolved / replan / manual_review
```

## Architecture

```text
CLI / API / ERPNext Webhook
        |
        v
FastAPI Control Plane
        |
        v
PostgreSQL
  - cases
  - tasks
  - events
  - approvals
  - tool_invocations
  - operators
  - case_lessons
        |
        v
Worker / Agent Runtime
        |
        +--> CaseContextBuilder
        +--> ToolRegistry + ToolProfileRouter
        +--> BusinessReadTools
        +--> LLMGateway
        +--> ActionRegistry
        +--> EvidenceGrounding
        +--> PolicyEngine
        +--> ExecutorRegistry
        +--> Verifier
        |
        v
External adapters
  - ERPNextAdapter today
  - SAP/WMS/CRM/ticketing adapters later
```

The CLI is only a presentation layer. It calls ResolveOps APIs and never talks to ERPNext directly. This keeps policy, audit, fault-injection gates and approval rules on the server side.

## Tool design

ResolveOps separates tools into two categories.

### Read tools

Read tools are LLM-callable business tools. They have explicit metadata:

```text
name
description
input_schema
permission
risk_level
side_effect = none
source_system
```

Examples:

- `get_order`
- `get_inventory`
- `get_transfer_options`
- `get_inbound_purchase`
- `get_reference_price`
- `get_customer_profile`

The Agent does not see every tool. Runtime selects the minimum tool profile for the current `event_type`.

### Write actions

Write operations are still tools in the broader engineering sense, but they are not directly exposed as LLM-callable functions.

The LLM can only propose a structured action plan. The runtime then validates, approves and executes it.

Examples:

- `transfer_stock`
- `create_purchase_request`
- `create_price_review_ticket`
- `create_supplier_followup_task`
- `draft_customer_notification`
- `create_manual_ticket`

This prevents the model from directly writing ERP data just because it generated a tool call.

## Safety model

ResolveOps uses a layered safety model:

```text
LLM proposes
-> action schema validation
-> evidence grounding
-> policy check
-> approval binding
-> executor preflight
-> idempotent write
-> read-after-write verification
```

Important rule:

```text
The model can propose an action.
The model cannot authorize itself to execute that action.
```

## Fault injection

Fault injection is used to verify that the Agent stops safely when the business state changes.

Example:

```text
1. Agent plans to transfer stock from source warehouse.
2. Approval is granted.
3. Before execution, fault injection changes the ERPNext source inventory.
4. Executor preflight re-reads ERPNext.
5. Approval is invalidated.
6. Agent replans or moves to manual_review.
```

CLI-triggered fault injection still goes through ResolveOps:

```text
resolveops.py fi run ...
-> ResolveOps API
-> permission / environment gate
-> ERPNextAdapter
-> ERPNext REST API
-> Stock Reconciliation
-> Case event + audit log
```

Fault injection is forbidden in production and requires `ENABLE_FAULT_INJECTION=true`.

## Quick start

For a more detailed guide, see [docs/quickstart.md](docs/quickstart.md).

Clone:

```powershell
git clone https://github.com/Chaofei-liu666/resolveops.git
cd resolveops
```

Copy environment template:

```powershell
Copy-Item .env.example .env
```

Edit `.env`:

```text
APP_ENV=local
ERPNEXT_BASE_URL=...
ERPNEXT_API_KEY=...
ERPNEXT_API_SECRET=...
WEBHOOK_SECRET=...
OPERATOR_API_KEY=...
LLM_BASE_URL=...
LLM_API_KEY=...
LLM_MODEL=...
```

Start services:

```powershell
docker compose up -d --build
```

Health check:

```powershell
Invoke-RestMethod http://localhost:8090/healthz
Invoke-RestMethod http://localhost:8090/readyz
```

Open:

```text
Swagger: http://localhost:8090/docs
Console: http://localhost:8090
```

## CLI usage

Initialize local CLI config:

```powershell
python resolveops.py init
python resolveops.py config set api_url http://localhost:8090
python resolveops.py config set operator_key <ops-admin-key>
python resolveops.py config show
```

`config show` masks the operator key. Case commands still require an explicit `<case-id>` so different business Cases do not accidentally share context.

On Windows, you can also double-click `resolveops.cmd` in the project directory. It opens a terminal, initializes the local CLI config if needed, checks runtime status, and leaves the terminal open for the next command. If authentication fails, edit:

```text
C:\Users\<you>\.resolveops\config.json
```

Check runtime:

```powershell
python resolveops.py status
```

Create and inspect a Case:

```powershell
python resolveops.py case create --type inventory_shortage --order SAL-ORD-2026-00002 --reason "manual CLI test"
python resolveops.py case list
python resolveops.py case show <case-id>
python resolveops.py case watch <case-id>
python resolveops.py case chat <case-id>
```

`case watch` prints a live, colorized Case event trace. It shows read-tool calls, Agent decision summaries, approvals, executor activity, verification and safe handoff events without giving the CLI direct ERPNext access.

`case chat` opens an interactive Case-scoped Agent session. Free-text input is sent to the read-only Case question endpoint; slash commands such as `/show`, `/events` and `/exit` stay in the CLI layer. General no-tool chat is allowed, but business tools remain scoped to Case questions and never execute writes.

The human CLI output is concise by default. Use `--verbose` on `case ask` or `case chat` to show rationale, used evidence and safe next steps.

Ask a Case-scoped read-only Agent question:

```powershell
python resolveops.py case ask <case-id> "Why not create a purchase request?"
python resolveops.py case ask <case-id> "If the source warehouse has no stock now, what should happen?"
```

`case ask` may call read tools to refresh evidence, but it never creates approvals or executes writes.

Approve or revoke an action:

```powershell
python resolveops.py approval approve <approval-id>
python resolveops.py approval revoke <approval-id> --reason "operator cancelled unsafe action"
```

Evaluate Agent execution:

```powershell
python resolveops.py eval summary --limit 20
python resolveops.py eval summary --limit 20 --cases
python resolveops.py eval case <case-id>
python resolveops.py eval case <case-id> --events
```

Run fault injection in local/test/staging:

```powershell
python resolveops.py fi list

python resolveops.py fi run inventory_changed_before_execution `
  --case <case-id> `
  --item SKU-A12 `
  --warehouse "重庆仓 - ROPS" `
  --new-qty 0 `
  --reason "simulate stock consumed before approval execution"
```

## Evaluation

ResolveOps evaluates Agent behavior from actual execution trails:

| Metric | Meaning |
|---|---|
| Case resolution rate | How many Cases ended as `resolved` |
| Verification pass rate | Whether write Cases were independently verified |
| Average read tool calls | How much investigation the Agent performed |
| Tool failure rate | How often read tools failed |
| Replanned Cases | Whether the Agent recovered from changed business state |
| Manual handoff Cases | Whether the Agent stopped instead of guessing |
| Policy denials | Whether unsafe actions were blocked |
| Evidence grounding failures | Whether unsupported plans were rejected |
| Context isolation failures | Whether cross-Case leakage was detected |

Example:

```powershell
python resolveops.py eval summary --limit 20
```

Example single Case inspection:

```powershell
python resolveops.py eval case <case-id>
```

## Tests

Local tests:

```powershell
python -m pytest -q
```

Containerized regression:

```powershell
.\scripts\test.ps1
```

Equivalent command:

```powershell
docker compose --profile test run --rm test
```

Latest local regression during development:

```text
79 passed, 1 skipped
```

## Production status

ResolveOps is currently suitable for:

```text
local development
job/project demonstration
ERPNext sandbox runs
```

It is not yet a drop-in production system for real ERP write operations. Before production write access, add enterprise IAM, managed secrets, monitoring and alerting, backup/restore, load testing, least-privilege ERP roles, and an operational incident runbook.

See [docs/production-readiness.md](docs/production-readiness.md).

## What is intentionally not included

ResolveOps intentionally avoids:

- a large multi-Agent role-play setup;
- a vector database by default;
- automatic prompt/skill rewriting;
- direct browser automation as the main ERP integration path;
- direct LLM access to ERP write APIs;
- cloud deployment instructions in the main path.

These can be added later if the use case justifies them. The current focus is reliable Agent execution against a real business system.

## Documentation

- [Architecture](docs/architecture.md)
- [Quickstart](docs/quickstart.md)
- [Runbook](docs/runbook.md)
- [Deployment safety checklist](docs/deployment.md)
- [Production readiness assessment](docs/production-readiness.md)
- [Reliability evaluation and fault injection](docs/evals.md)
- [Interview notes](docs/interview-notes.md)

## Security

Do not commit `.env`, local databases, real ERPNext credentials, LLM keys or operator keys.

See [SECURITY.md](SECURITY.md).

## License

MIT License. See [LICENSE](LICENSE).
