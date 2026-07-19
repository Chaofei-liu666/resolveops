# ResolveOps

ResolveOps is an API-first enterprise Agent runtime for handling business exceptions in ERP workflows.

It focuses on cases where a deterministic workflow is not enough: missing inventory, price mismatch, supplier delay, approval gating, business-state changes, and execution verification.

Current reference integration: ERPNext.

ERPNext is used as a real open-source ERP sandbox and adapter target. The Agent architecture is not ERPNext-specific: the same Tool Registry, Policy Engine, Executor and Verifier boundaries can be adapted to SAP, WMS, CRM, ticketing systems or internal business platforms.

## What it demonstrates

- Agent investigation with read-only business tools.
- Dynamic tool exposure by case type.
- Evidence-grounded action planning.
- Policy-controlled write execution.
- Human approval bound to case, plan version and action hash.
- Approval expiration and revocation.
- Idempotent write actions.
- PostgreSQL-backed durable case state.
- Case-level context isolation.
- Read-after-write verification.
- Controlled fault injection against ERPNext sandbox data.
- CLI-first developer experience.
- Regression and fault-injection tests.

## Supported case types

| Case type | Description |
|---|---|
| `inventory_shortage` | Sales order cannot be fulfilled from the current warehouse. The Agent investigates inventory, transfer routes, purchase options and customer constraints. |
| `price_mismatch` | Sales order price differs from reference price. The Agent creates a controlled price-review record instead of changing ERP prices directly. |
| `delivery_delay` | Inbound supply arrives later than the customer delivery date. The Agent creates a controlled supplier-follow-up task instead of directly changing ERP dates. |

## Core flow

```text
Business exception
-> Case
-> Read tool investigation
-> Evidence-grounded Action Plan
-> Policy Engine
-> Approval
-> Executor
-> Read-after-write Verification
-> resolved / replan / manual_review
```

## Architecture

```text
CLI / Swagger / ERPNext Webhook
        -> FastAPI Control Plane
        -> PostgreSQL Case State
        -> Worker / Agent Runtime
        -> Tool Registry -> Policy Engine -> Executor -> Verifier
        -> ERPNextAdapter / future external-system adapters
```

The CLI is only a presentation layer. It calls ResolveOps APIs and does not talk to ERPNext directly.

## Quick start

For a detailed guide, see [Quickstart](docs/quickstart.md).

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

OpenAPI docs:

```text
http://localhost:8090/docs
```

Console:

```text
http://localhost:8090
```

## CLI

Set API address and operator key:

```powershell
$env:RESOLVEOPS_API_URL="http://localhost:8090"
$env:RESOLVEOPS_OPERATOR_KEY="<ops-admin-key>"
```

Runtime status:

```powershell
python resolveops.py status
```

Create and inspect a Case:

```powershell
python resolveops.py case create --type inventory_shortage --order SAL-ORD-2026-00002 --reason "manual CLI test"
python resolveops.py case list
python resolveops.py case show <case-id>
```

Evaluate recent Agent runs:

```powershell
python resolveops.py eval summary --limit 20
python resolveops.py eval summary --limit 20 --cases
```

Approve or revoke an action:

```powershell
python resolveops.py approval approve <approval-id>
python resolveops.py approval revoke <approval-id> --reason "operator cancelled unsafe action"
```

Fault injection catalog:

```powershell
python resolveops.py fi list
```

Trigger ERPNext sandbox stock change through ResolveOps:

```powershell
python resolveops.py fi run inventory_changed_before_execution `
  --case <case-id> `
  --item SKU-A12 `
  --warehouse "重庆仓 - ROPS" `
  --new-qty 0 `
  --reason "simulate stock consumed before approval execution"
```

This calls:

```text
CLI
-> ResolveOps API
-> permission / environment gate
-> ERPNextAdapter
-> ERPNext REST API
-> Stock Reconciliation
```

Fault injection is forbidden in production and requires:

```text
ENABLE_FAULT_INJECTION=true
APP_ENV != production
ops_admin or config_admin operator
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

Current containerized regression:

```text
64 passed, 1 skipped
```

## Production status

ResolveOps is currently suitable for:

```text
local development
job/project demonstration
ERPNext sandbox runs
```

It is not yet a drop-in production system for real ERP write operations. Before production write access, add enterprise IAM, secret management, monitoring and alerting, backup/restore, load testing, and an operational runbook for incident handling.

See [production readiness](docs/production-readiness.md).

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
