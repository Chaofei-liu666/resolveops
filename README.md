# ResolveOps

ResolveOps is a small API-first Agent runtime for order fulfillment exceptions.

The project starts from a narrow problem:

```text
An ERP system has already detected an exception.
The normal deterministic workflow cannot safely decide the next step.
ResolveOps investigates, proposes an evidence-grounded plan, requests approval when needed,
executes controlled actions, and verifies the final business state.
```

Current reference integration: ERPNext.

ERPNext is used as the current open-source ERP sandbox and system of record. The code keeps the ERPNext adapter behind tool/action boundaries so the same runtime shape can be adapted to other business systems later.

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

That boundary is the main design choice: the model can investigate and propose, but write access is still controlled by deterministic code.

## Current coverage

- Case-based durable execution, not one-off chat completion.
- Read-only business tools exposed by schema and case type.
- Dynamic tool profile routing instead of exposing every tool to the LLM.
- Evidence-grounded planning with deterministic validation before approval.
- Bounded plan self-correction when evidence grounding fails.
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
- Textual Agent Workbench for operators plus non-interactive developer commands.
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
Agent Workbench / API / ERPNext Webhook
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
        +--> Agent primitives: LLMRequest / LLMResult / ToolSchema
        +--> Agent runtime: message boundary + bounded read-tool loop
        +--> ResolveOps domain: CaseProfile + ToolRegistry + ActionRegistry
        +--> CaseContextBuilder
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

The Agent Workbench is only a presentation layer. It calls ResolveOps APIs and never talks to ERPNext directly. This keeps policy, audit, fault-injection gates and approval rules on the server side.

The runtime uses a small three-layer dependency direction rather than a large
generic framework:

```text
Agent primitives (provider-neutral contracts)
  -> Agent runtime (bounded LLM -> read tool -> observation loop)
    -> ResolveOps domain (CaseProfile, prompts, evidence, policy and actions)

Adapters stay outside this stack: LLMGateway / ERPNextAdapter / PostgreSQL / FastAPI / Workbench.
```

`ToolSchema` is the LLM-visible minimum; `ToolSpec` adds ResolveOps execution
and governance metadata. Write actions are still tools, but are intentionally
not LLM-callable: they move through grounding, Policy, Approval, Preflight,
Executor and read-after-write verification.

## First run

```powershell
Copy-Item .env.example .env
docker compose up -d --build
python resolveops.py init
python resolveops.py config set operator_key local-ops-key
python resolveops.py doctor
python resolveops.py sandbox check
python -m pip install -r requirements-cli.txt
python resolveops.py console
```

For a full ERPNext-backed demo, configure `.env` with ERPNext and LLM credentials, then run:

```powershell
python resolveops.py sandbox seed
python resolveops.py case create --type inventory_shortage --order SAL-ORD-2026-00002 --reason "sandbox demo"
```

See [docs/quickstart.md](docs/quickstart.md) for the detailed setup path.

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
POSTGRES_PASSWORD=resolveops
DATABASE_URL=postgresql+psycopg://resolveops:resolveops@postgres:5432/resolveops
WEBHOOK_SECRET=local-webhook-secret
OPERATOR_API_KEY=local-ops-key
```

For full ERPNext and LLM runs, also configure:

```text
ERPNEXT_BASE_URL=...
ERPNEXT_API_KEY=...
ERPNEXT_API_SECRET=...
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
Agent Workbench: python resolveops.py console
```

## Workbench and CLI setup

Initialize local CLI config:

```powershell
python resolveops.py init
python resolveops.py config set api_url http://localhost:8090
python resolveops.py config set operator_key <ops-admin-key>
python resolveops.py config show
```

`config show` masks the operator key. Case commands still require an explicit `<case-id>` so different business Cases do not accidentally share context.

On Windows, you can also double-click `resolveops.cmd` in the project directory. It initializes the local CLI config if needed, installs the small Textual Workbench dependency on first use, starts Docker services, waits for the API, and opens the ResolveOps Agent Workbench. If authentication fails, edit:

```text
C:\Users\<you>\.resolveops\config.json
```

The launcher does not rebuild images every time. After changing backend code, run:

```powershell
docker compose up -d --build api worker
```

Check runtime:

```powershell
python resolveops.py status
```

Reset the local ERPNext demo state before a repeated inventory-shortage run:

```bash
python resolveops.py sandbox seed
```

This restores the demo source warehouse to `40` units and target warehouse to `0` units through ResolveOps, so a completed transfer does not make the next run appear falsely unresolved.

Open the single interactive operator interface:

```powershell
python resolveops.py console
```

The Workbench has one persistent screen: the left pane lists Cases and the right pane contains the current conversation and Agent trace. It starts in `GENERAL` mode; free text is an operator-level LLM conversation with no ERP tools. Select a Case in the left pane or use `/focus <case-id>` to enter an explicit Case context. `/back` returns to `GENERAL` without closing the application.

Workbench commands:

```text
/new                     create a new Case with the form
/focus <case-id>         enter an explicit Case context
/back                    return to GENERAL
/refresh                 refresh runtime, Cases and active trace
/events                  expand or collapse active Case event detail
/eval                    append evaluation summary to the trace
/approve [approval-id]   confirm a pending approval
/revoke [approval-id]    revoke a pending approval with confirmation
/quit                    leave the Workbench
```

When a Case is active, questions use only that Case's context and may call its read tools. The Workbench never mixes transcripts between Cases. General and Case answers stream over SSE; background investigation instead appends durable lifecycle and business events, so token deltas and hidden reasoning are never persisted. Approval controls only call existing ResolveOps approval APIs; the server still enforces operator role, plan binding, preflight and verification.

Non-interactive commands remain useful for scripting and diagnosis:

```powershell
python resolveops.py case create --type inventory_shortage --order SAL-ORD-2026-00002 --reason "manual CLI test"
python resolveops.py case list
python resolveops.py case show <case-id>
python resolveops.py case ask <case-id> "Why not create a purchase request?"
```

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
python resolveops.py eval seed --suite core-v1 --order SAL-ORD-2026-00002
python resolveops.py eval summary --suite core-v1 --limit 50
python resolveops.py eval summary --limit 20
python resolveops.py eval summary --limit 20 --cases
python resolveops.py eval case <case-id>
python resolveops.py eval case <case-id> --events
```

Use `--suite` for reported metrics. Do not report numbers from a mixed historical database because it may include early debugging Cases, setup failures and old event formats.

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
| Argument correctness | Whether planned action parameters match observed ERP/tool evidence |
| Replanned Cases | Whether the Agent recovered from changed business state |
| Manual handoff Cases | Whether the Agent stopped instead of guessing |
| Policy denials | Whether unsafe actions were blocked |
| Evidence grounding failures | Whether unsupported plans were rejected |
| Context isolation failures | Whether cross-Case leakage was detected |
| LLM / Tool / Queue latency | Where execution time is spent: model generation, external tool/API calls, or Worker queueing |

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
105 passed, 1 skipped
```

## Current status

ResolveOps is currently suitable for:

```text
local development
technical project review
ERPNext sandbox runs
```

It is not yet a drop-in production system for real ERP write operations. Before production write access, add enterprise IAM, managed secrets, monitoring and alerting, backup/restore, load testing, least-privilege ERP roles, and an operational incident runbook.

See [docs/deployment.md](docs/deployment.md).

## What is intentionally not included

ResolveOps intentionally avoids:

- a large multi-Agent role-play setup;
- a vector database by default;
- automatic prompt/skill rewriting;
- direct browser automation as the main ERP integration path;
- direct LLM access to ERP write APIs;
- cloud deployment instructions in the main path.

These can be added later if the use case justifies them. The current focus is reliable execution against a real ERP sandbox.

## Documentation

- [Architecture](docs/architecture.md)
- [Quickstart](docs/quickstart.md)
- [Runbook](docs/runbook.md)
- [Deployment safety checklist](docs/deployment.md)
- [Reliability evaluation and fault injection](docs/evals.md)

## Security

Do not commit `.env`, local databases, real ERPNext credentials, LLM keys or operator keys.

See [SECURITY.md](SECURITY.md).

## License

MIT License. See [LICENSE](LICENSE).
