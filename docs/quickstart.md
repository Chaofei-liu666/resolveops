# Quickstart

This guide explains how to run ResolveOps after cloning the repository.

There are two paths:

```text
Path A: run ResolveOps only
Path B: run ResolveOps with an ERPNext sandbox
```

Path A is enough to inspect the API, use the CLI, and run tests. Path B is required for a real end-to-end business Case because ResolveOps reads and writes business documents through ERPNext.

## Prerequisites

Required:

- Docker Desktop or Docker Engine;
- Python 3.12+ if running the CLI/tests outside containers;
- Git.

Required for full sandbox runs:

- an existing ERPNext sandbox;
- ERPNext API key and secret;
- test Customer, Item, Warehouse, Sales Order and stock data.

ResolveOps does not currently bundle ERPNext in its own Docker Compose file.

## Path A: run ResolveOps only

Clone and enter the repository:

```bash
git clone https://github.com/<your-org>/resolveops.git
cd resolveops
```

Copy environment template:

```bash
cp .env.example .env
```

PowerShell:

```powershell
Copy-Item .env.example .env
```

For local inspection, keep:

```text
APP_ENV=local
POSTGRES_PASSWORD=resolveops
WEBHOOK_SECRET=local-webhook-secret
OPERATOR_API_KEY=local-ops-key
```

Start services:

```bash
docker compose up -d --build
```

Health check:

```bash
curl http://localhost:8090/healthz
```

PowerShell:

```powershell
Invoke-RestMethod http://localhost:8090/healthz
```

Open:

```text
http://localhost:8090/docs
http://localhost:8090
```

Set CLI environment:

```bash
export RESOLVEOPS_API_URL=http://localhost:8090
export RESOLVEOPS_OPERATOR_KEY=local-ops-key
```

PowerShell:

```powershell
$env:RESOLVEOPS_API_URL="http://localhost:8090"
$env:RESOLVEOPS_OPERATOR_KEY="local-ops-key"
```

Use the CLI:

```bash
python resolveops.py status
python resolveops.py case list
```

Run tests:

```bash
python -m pytest -q
docker compose --profile test run --rm test
```

## Path B: run with ERPNext sandbox

Prepare or reuse an ERPNext sandbox.

Configure `.env`:

```text
APP_ENV=local
ERPNEXT_BASE_URL=http://<erpnext-host>:8000
ERPNEXT_API_KEY=<api-key>
ERPNEXT_API_SECRET=<api-secret>
OPERATOR_API_KEY=<ops-admin-key>
WEBHOOK_SECRET=<webhook-secret>
LLM_BASE_URL=<chat-completions-compatible-base-url>
LLM_API_KEY=<llm-api-key>
LLM_MODEL=<model>
```

The ERPNext integration user should be able to read Sales Order, Customer, Item, Item Price, Warehouse/Bin and Purchase Order. For sandbox write tests, it also needs permission to create the draft/test documents used by ResolveOps.

If ERPNext runs in a different Docker network, make sure `ERPNEXT_BASE_URL` is reachable from inside the ResolveOps containers.

Restart:

```bash
docker compose up -d --build
```

Check readiness:

```bash
curl http://localhost:8090/readyz
```

Create a Case:

```bash
python resolveops.py case create --type inventory_shortage --order SAL-ORD-2026-00002 --reason "sandbox run"
python resolveops.py case list
python resolveops.py case show <case-id>
```

Evaluate recent Agent runs:

```bash
python resolveops.py eval summary --limit 20
python resolveops.py eval summary --limit 20 --cases
python resolveops.py eval case <case-id>
python resolveops.py eval case <case-id> --events
```

Approve pending actions:

```bash
python resolveops.py approval approve <approval-id>
```

Then inspect the Case and the corresponding ERPNext business document.

## Fault injection from CLI

Fault injection is optional and should only be used in local/test/staging.

In `.env`:

```text
ENABLE_FAULT_INJECTION=true
ERPNEXT_COMPANY=<company-name>
ERPNEXT_STOCK_DIFFERENCE_ACCOUNT=<difference-account>
ERPNEXT_DEFAULT_VALUATION_RATE=100
```

Restart:

```bash
docker compose up -d --build
```

List available faults:

```bash
python resolveops.py fi list
```

Change ERPNext sandbox stock through ResolveOps:

```bash
python resolveops.py fi run inventory_changed_before_execution \
  --case <case-id> \
  --item SKU-A12 \
  --warehouse "重庆仓 - ROPS" \
  --new-qty 0 \
  --reason "simulate stock consumed before approval execution"
```

This does not open ERPNext UI. The chain is:

```text
CLI -> ResolveOps API -> ERPNextAdapter -> ERPNext REST API -> Stock Reconciliation
```

## Common problems

### `/readyz` is degraded

Use:

```bash
python resolveops.py status --json
```

Check ERPNext credentials, LLM credentials, operator key, migrations, and queued/failed tasks.

### CLI returns 401

Set:

```bash
export RESOLVEOPS_OPERATOR_KEY=<OPERATOR_API_KEY from .env>
```

PowerShell:

```powershell
$env:RESOLVEOPS_OPERATOR_KEY="<OPERATOR_API_KEY from .env>"
```

### Fault injection returns 403

Check:

```text
APP_ENV must not be production
ENABLE_FAULT_INJECTION must be true
operator role must be ops_admin or config_admin
```
