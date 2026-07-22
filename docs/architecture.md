# ResolveOps architecture

ResolveOps is an API-first Agent runtime for order fulfillment exception handling.

It is not designed as a generic ERP Agent. ERPNext is the current reference sandbox and adapter target, but the main architectural boundaries are business-tool oriented and can be adapted to other systems.

## Design boundary

ResolveOps should not replace deterministic business workflows.

```text
Normal deterministic process -> ERP / workflow engine
Long-tail exception process  -> ResolveOps Agent
```

Examples of normal deterministic processes:

```text
payment received -> reserve stock -> create delivery note -> notify warehouse
```

Examples of exception processes:

```text
order cannot be fulfilled
price differs from reference price
inbound supply arrives after the customer delivery date
```

The Agent is useful when the next step depends on scattered evidence, business constraints, and risk-based approval.

## Current supported case types

| Case type | Current purpose |
|---|---|
| `inventory_shortage` | Investigate shortage and choose between transfer, purchase request, notification draft, or manual review |
| `price_mismatch` | Investigate order/reference price mismatch and create a controlled price review record |
| `delivery_delay` | Investigate inbound delay and create a controlled supplier follow-up task |

`inventory_shortage` currently has the deepest end-to-end coverage.

## High-level flow

```text
External event / CLI / API
  -> Case
  -> Task
  -> Worker
  -> CaseContextBuilder
  -> Tool Profile Router
  -> Read Tool Scheduler
  -> LLM Planner
  -> Action normalization
  -> Evidence Grounding
  -> Policy Engine
  -> Bound Approval
  -> Governed Executor
  -> Read-after-write Verification
  -> resolved / waiting_approval / manual_review / replan
```

## Runtime components

```text
FastAPI Control Plane
  - webhook/API ingress
  - case creation and idempotency
  - approval APIs
  - fault injection APIs
  - runtime status
  - eval APIs

PostgreSQL
  - cases
  - tasks
  - events
  - approvals
  - tool_invocations
  - operators
  - logistics_lanes
  - price_reviews
  - supplier_followups
  - case_lessons
  - schema_migrations

Worker
  - task leasing
  - investigation
  - execution
  - retry/manual-review transitions

Agent runtime
  - context builder
  - tool registry
  - tool profile router
  - read tool scheduler
  - LLM gateway
  - action registry
  - evidence validator
  - policy engine
  - executor registry
  - verifier
```

## Why it is not split into multiple Agents yet

The current implementation uses one planning Agent plus deterministic runtime boundaries.

The project intentionally does not create separate role-play Agents such as:

```text
Inventory Agent
Purchase Agent
Approval Agent
Supervisor Agent
```

At the current scale, those roles would mostly share the same model, context and tools. Splitting them would add latency and debugging complexity without improving the core safety model.

The current split is instead based on engineering responsibilities:

```text
Case type
-> read tool profile
-> action profile
-> evidence validator
-> policy
-> approval
-> executor
-> verifier
```

Specialized Agents would only make sense later if different domains require clearly different:

- tool sets;
- context scopes;
- approval policies;
- memory/evaluation metrics;
- responsible teams;
- runtime budgets.

## Tool design

ResolveOps separates read tools and write actions.

### Read tools

Read tools are LLM-callable and represented by `ToolSpec`.

Each read tool declares:

```text
name
description
input_schema
permission
side_effect
risk_level
source_system
requires_approval
```

Current examples:

| Tool | Purpose |
|---|---|
| `get_order` | Read the current Sales Order |
| `get_inventory` | Read stock information |
| `get_transfer_options` | Read feasible transfer lanes and cost/time facts |
| `get_inbound_purchase` | Read inbound purchase facts |
| `get_item_supply_profile` | Read lead time and supply profile |
| `get_reference_price` | Read reference price for price mismatch |
| `get_customer_profile` | Read customer constraints and risk signals |

Runtime only exposes tools relevant to the current `event_type`.

Example:

| Case type | Read tool profile |
|---|---|
| `inventory_shortage` | `get_order`, `get_inventory`, `list_alternative_warehouses`, `get_customer_profile`, `get_item_supply_profile`, `get_inbound_purchase`, `get_transfer_options` |
| `price_mismatch` | `get_order`, `get_reference_price`, `get_customer_profile` |
| `delivery_delay` | `get_order`, `get_inbound_purchase`, `get_item_supply_profile`, `get_customer_profile` |

This is not just a prompt instruction. Hidden tools are not exposed to the LLM schema, and runtime policy still rejects disabled tools.

### Write actions

Write actions are not directly callable LLM tools.

The LLM may propose a structured Action Plan. The runtime validates and executes that plan through deterministic gates.

Current examples:

| Action | Execution target |
|---|---|
| `transfer_stock` | ERPNext Stock Entry draft |
| `create_purchase_request` | ERPNext Material Request draft |
| `create_price_review_ticket` | ResolveOps local PriceReview record |
| `create_supplier_followup_task` | ResolveOps local SupplierFollowup record |
| `draft_customer_notification` | ResolveOps controlled draft action |
| `create_manual_ticket` | Manual review handoff |

This boundary is deliberate:

```text
The LLM proposes a write action.
The runtime decides whether that action is valid, approved and executable.
```

## LLM gateway

The Agent does not call provider-specific APIs directly. It uses `LLMGateway`.

The gateway is responsible for:

- calling a chat-completions compatible endpoint;
- injecting the configured model name;
- setting provider timeout;
- normalizing timeout, HTTP and provider errors into `LLMResult`;
- recording model, latency and usage metadata;
- making LLM failure a safe handoff instead of an unsafe write.

Production extensions can be added at this boundary:

```text
request queue
token bucket
per-provider max concurrency
exponential backoff
circuit breaker
fallback model
```

## Case context and isolation

ResolveOps builds context by `case_id`, not by chat session.

`CaseContextBuilder` assembles:

```text
scope: case_id, tenant_id, event_type, order_id, plan_version
current_state
task_context
confirmed_observations
previous_plan
last_failure
recent_events
approval_refs
invocation_refs
long_term_memory
```

Context isolation matters because multiple Cases may run concurrently.

Guards:

- Task payload is treated only as scheduling context, not as identity source.
- Foreign `case_id`, `tenant_id` or `order_id` from task context is removed before LLM planning.
- Durable records from other Cases cause `manual_review` instead of continuing.
- `get_order` observations must match the current `scope.order_id`.

This prevents one Case from accidentally using another Case's approval, invocation, evidence or task payload.

## Read tool scheduler

Read investigation is executed through `ReadToolScheduler`.

Current behavior:

- schedules only read tools;
- deduplicates identical tool-name plus argument calls in the same batch;
- reuses already seen read results;
- uses `ThreadPoolExecutor` because the current ERPNext adapter uses synchronous HTTP calls;
- wraps each tool result as `ToolResult`;
- converts exceptions into structured failed tool results instead of breaking the Agent loop.

Write actions do not go through the read scheduler. They go through:

```text
Action Plan
-> Evidence Grounding
-> Policy
-> Approval
-> Executor
-> Verification
```

## Evidence grounding

The LLM can propose a plan, but deterministic code checks whether the plan is supported by evidence.

Examples:

| Action | Required evidence |
|---|---|
| `transfer_stock` | Sales Order, target inventory, source inventory with enough usable stock, configured transfer lane |
| `create_purchase_request` | Sales Order, target inventory, item supply profile, inbound purchase check |
| `create_price_review_ticket` | Sales Order item rate, same-SKU reference price, non-zero difference |
| `create_supplier_followup_task` | Sales Order delivery date, same-SKU inbound purchase, supplier, inbound schedule date later than customer delivery date |
| multi-action plan | Combined quantity must cover the computed shortage when applicable |

Unsupported plans do not create approvals.

## Policy and approval

The Policy Engine decides whether an action is allowed and which roles must approve it.

Operator identity is resolved from the database:

```text
X-Operator-Key
-> sha256
-> operators.api_key_hash
-> subject / role / tenant_id
```

API routes do not trust caller-provided role headers.

Approval is bound to:

```text
case_id
plan_version
action_hash
action input
required_roles
expires_at
```

Execution re-checks:

- approval status;
- expiry;
- revocation;
- plan version;
- action hash;
- executor availability;
- external business preconditions.

This prevents approval replay, parameter tampering and stale approvals.

## Executor and verification

The Executor Registry performs governed write execution.

Current implementations:

| Action | Executor |
|---|---|
| `transfer_stock` | ERPNext Stock Entry draft |
| `create_purchase_request` | ERPNext Material Request draft |
| `create_price_review_ticket` | local PriceReview draft |
| `create_supplier_followup_task` | local SupplierFollowup draft |

Write success does not automatically resolve the Case.

After a write, ResolveOps performs read-after-write verification:

- does the record exist?
- do important fields match the intended action?
- is the status acceptable?
- did the external system state actually change as expected?

Only verified writes can move a Case to `resolved`.

## Replanning and manual review

If executor preflight detects that business state changed, ResolveOps invalidates the old approval and queues replanning.

Example:

```text
planned transfer from source warehouse
-> approval granted
-> source inventory changed before execution
-> executor preflight fails
-> old approval invalidated
-> investigation task queued
-> new plan or manual_review
```

If the Agent cannot produce a safe executable plan, the Case enters `manual_review`.

## Lightweight memory

ResolveOps includes lightweight verified Case lessons.

It is not a full vector memory system and does not store complete chat history.

Lessons are recorded only from verified outcomes:

```text
Case status == resolved
write action verification passed
```

Lessons are planning hints only. They do not replace:

- real-time ERP queries;
- evidence grounding;
- policy;
- approval;
- idempotency;
- verification.

## Schema migration

ResolveOps uses a lightweight SQL migration runner:

```text
production/migrations/*.sql
-> ordered by filename
-> recorded in schema_migrations(version, filename, checksum, applied_at)
```

API and Worker both call the same migration runner during startup. PostgreSQL advisory lock prevents two processes from running migrations concurrently.

For production, migrations should normally run as a separate deployment step with a dedicated migration database role.

## Runtime evaluation

ResolveOps evaluates Agent behavior from actual Case event trails.

Evaluation APIs:

```text
GET /v1/evals/summary?limit=50
GET /v1/evals/cases/{case_id}
Required role: ops_admin or config_admin
```

Metrics include:

- Case resolution rate;
- tool call count;
- tool failure rate;
- approval waiting count;
- verification pass rate;
- policy denial count;
- evidence grounding pass/failure;
- context isolation pass/failure;
- replanned Cases;
- manual handoff Cases.

The CLI exposes these metrics:

```powershell
python resolveops.py eval summary --limit 20
python resolveops.py eval case <case-id>
```
