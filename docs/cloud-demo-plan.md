# Cloud Demo Plan

This document describes a hosted sandbox demo for ResolveOps.

Goal:

```text
Let reviewers experience the full Agent loop without installing ERPNext locally.
```

Non-goal:

```text
Do not provide a production SaaS service.
Do not process real customer, order, financial or ERP data.
```

## Positioning

The cloud demo should be described as:

```text
Hosted ERPNext + ResolveOps sandbox demo
```

Not as:

```text
production deployment
cloud SaaS
managed ERP automation service
```

The purpose is to demonstrate:

- real business-system integration;
- Agent read-tool investigation;
- action planning;
- approval;
- controlled write execution;
- read-after-write verification;
- case trace and auditability.

## Recommended server

Minimum for a short-lived demo:

```text
4 vCPU
8 GB RAM
80 GB SSD
Ubuntu 22.04 / 24.04
```

More comfortable:

```text
4 vCPU
16 GB RAM
100 GB SSD
Ubuntu 22.04 / 24.04
```

Reason: ERPNext is heavier than ResolveOps. ERPNext usually needs MariaDB, Redis and Frappe/ERPNext workers. ResolveOps adds PostgreSQL, API and Worker.

## High-level topology

```text
Internet
   |
   v
Caddy / Nginx HTTPS reverse proxy
   |
   +-- ResolveOps console / API
   |
   +-- ERPNext sandbox

Cloud VM
   |
   +-- ResolveOps API
   +-- ResolveOps Worker
   +-- ResolveOps PostgreSQL
   +-- ERPNext / Frappe
   +-- MariaDB
   +-- Redis
   +-- persistent volumes
```

## Public endpoints

Recommended public routes:

```text
https://resolveops-demo.example.com        -> ResolveOps console
https://resolveops-demo.example.com/docs   -> ResolveOps OpenAPI docs
https://erpnext-demo.example.com           -> ERPNext sandbox
```

If there is no domain yet, use the server IP only temporarily.

HTTPS is recommended even for a demo because the system uses operator keys and ERPNext login sessions.

## Access control

Do not leave the demo fully open.

Recommended:

- protect ResolveOps with operator key;
- create limited demo ERPNext users;
- do not expose ERPNext Administrator credentials;
- do not publish real API keys in README, issues or screenshots;
- rotate demo keys after interviews or public sharing.

Optional:

- HTTP basic auth at reverse proxy;
- IP allowlist during interviews;
- short-lived demo account credentials.

## Environment settings

Recommended `.env` for hosted demo:

```text
APP_ENV=staging
ENABLE_FAULT_INJECTION=false
OPERATOR_SEED_KEYS=
```

Use real non-placeholder values for:

```text
POSTGRES_PASSWORD
ERPNEXT_BASE_URL
ERPNEXT_API_KEY
ERPNEXT_API_SECRET
WEBHOOK_SECRET
OPERATOR_API_KEY
LLM_BASE_URL
LLM_API_KEY
LLM_MODEL
```

Fault injection should stay disabled for a public demo. If a fault-injection demo is needed, run it locally or behind a private access gate.

## Demo case catalog

Pre-seed a small set of stable cases.

| Demo ID | Scenario | What it shows |
|---|---|---|
| DEMO-01 | Inventory shortage with transfer | read tools, stock evidence, approval, draft transfer, verification |
| DEMO-02 | Inventory shortage with transfer + purchase request | multi-action planning and shortage coverage |
| DEMO-03 | Price mismatch | dynamic tool/action profile; no inventory tools used |
| DEMO-04 | Delivery delay | supplier follow-up flow; non-stock case type |
| DEMO-05 | Approval expiration/revoke | approval lifecycle and safe stop |
| DEMO-06 | Business state changed before write | preflight check and replan/manual review |

The public demo does not need to let every visitor freely mutate all data. It is safer to provide fixed demo flows or resettable demo accounts.

## What to disable in public demo

Disable or hide:

- fault injection endpoint;
- arbitrary configuration writes;
- dangerous ERPNext admin actions;
- production-like ERP credentials;
- unrestricted write actions;
- open operator key sharing;
- real customer or financial data.

Keep allowed:

- viewing cases;
- creating predefined demo cases;
- approving safe demo actions with limited roles;
- inspecting tool trace;
- inspecting events and verification result.

## Reset strategy

The demo should be resettable.

Minimum:

```text
manual reset script
```

Better:

```text
nightly reset
```

Reset should:

- restore ERPNext demo stock levels;
- delete or archive generated demo documents;
- clear ResolveOps case/task/approval data if needed;
- preserve seed configuration;
- rotate temporary public credentials when necessary.

Do not reset by deleting Docker volumes unless you have tested the bootstrap path.

## Deployment steps

High-level sequence:

1. Rent a cloud VM.
2. Install Docker and Docker Compose.
3. Deploy ERPNext sandbox.
4. Create ERPNext test company, users, items, customers, warehouses and orders.
5. Generate ERPNext API key/secret for ResolveOps integration user.
6. Deploy ResolveOps.
7. Configure `.env` with `APP_ENV=staging` and production-like non-placeholder secrets.
8. Configure reverse proxy and HTTPS.
9. Run `/healthz`, `/readyz`, and `python resolveops.py status`.
10. Run the demo case catalog.
11. Record screenshots or a short demo video.
12. Document public demo credentials and reset policy privately.

## Validation checklist

Before sharing the demo URL:

- `/healthz` returns ok;
- `/readyz` returns ready;
- `python resolveops.py status` shows no configuration errors;
- ERPNext sandbox is reachable from ResolveOps containers;
- LLM calls work;
- `DEMO-01` can reach approval or resolved state;
- no real secrets appear in logs, README or browser screenshots;
- `ENABLE_FAULT_INJECTION=false`;
- demo users cannot access administrative ERPNext settings.

## Interview positioning

Recommended wording:

> I deployed ResolveOps and ERPNext together as a hosted sandbox demo so reviewers can experience the full business exception loop online. It is not a production SaaS service: the environment uses demo data, restricted access, disabled fault injection, and a resettable sandbox. The production-readiness document separately lists the additional IAM, secret management, monitoring, backup and load-testing work required before real enterprise deployment.

