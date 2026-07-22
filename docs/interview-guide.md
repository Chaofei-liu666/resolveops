# Interview guide

This is a concise explanation of the project for interviews or project review.

## 30-second version

ResolveOps handles order fulfillment exceptions as durable Cases. It connects to ERPNext as a real business system, uses schema-defined read tools to collect evidence, asks an LLM to propose an action plan, then validates the plan through evidence grounding, policy checks, bound approval, idempotent execution and read-after-write verification.

The main point is not that the model can call an ERP API. The main point is that the model cannot directly authorize writes. It can investigate and propose; deterministic runtime layers decide whether an action is grounded, allowed, approved and verified.

## 2-minute version

The project focuses on long-tail order fulfillment exceptions, such as inventory shortage, price mismatch and delivery delay. Normal ERP workflows are deterministic and should stay in the ERP. ResolveOps only enters when the normal workflow cannot safely decide the next step.

The runtime is Case-based. A Case stores status, events, tool observations, plans, approvals and write invocations in PostgreSQL. That means recovery does not depend on chat history.

The Agent flow is:

```text
Case event
-> build scoped context
-> route read tool profile
-> call read-only tools
-> LLM proposes action plan
-> normalize action schema
-> validate evidence grounding
-> repair plan once if needed
-> policy check
-> approval
-> execute through controlled executor
-> verify by reading ERPNext again
```

Write actions are not exposed as direct LLM tools. They are structured plans that must pass deterministic checks before execution.

## Key design choices

### Why ERPNext?

ERPNext gives the project a real open-source ERP sandbox: Sales Order, Customer, Item, Warehouse, stock documents and REST APIs. This avoids a fake mock demo while keeping the system reproducible.

### Why not split into many Agents?

The current scale does not justify separate Inventory Agent, Purchase Agent or Approval Agent. The stronger boundary is not role-play; it is runtime responsibility:

```text
planner
tool registry
policy
approval
executor
verifier
evaluation
```

I would split into specialized Agents only when tool sets, permissions, context and evaluation metrics become clearly different.

### Why PostgreSQL instead of Redis for state?

Case state needs transactions, auditability and recovery. PostgreSQL stores cases, tasks, events, approvals and invocations. Redis would be suitable later for rate limiting or short-lived coordination, but not as the source of truth for business state.

### What makes it more than workflow + LLM?

There are three parts:

1. The LLM decides which evidence it needs through read tools.
2. The LLM proposes a plan from observed evidence instead of following a hard-coded action.
3. When deterministic grounding rejects the plan, the system can feed the structured problem back to the LLM for one bounded repair.

The deterministic workflow still exists, but it is used as a guardrail around the Agent, not as a replacement for the Agent.

## Plan repair explanation

Earlier, if the LLM produced an action with unsupported arguments, the Case moved directly to manual review. Now ResolveOps records the grounding problems and sends them back to a repair prompt together with existing observations and action schemas.

Rules:

```text
- no new ERP tool calls during repair
- no invented facts
- one repair attempt by default
- repaired plan must pass grounding again
```

This improved the fixed suite:

```text
core-v3: grounding_failures=9, evidence_faithfulness=70.0%
core-v4: grounding_failures=0, evidence_faithfulness=100.0%
```

The tradeoff is slightly higher token use and latency.

## Evaluation answer

I evaluate both final state and trajectory. Final state alone is not enough because an Agent can accidentally reach a correct-looking result through unsafe steps.

Current metrics include:

```text
task success
tool selection
action argument correctness
evidence faithfulness
verification pass
read tool calls
token use
LLM/tool latency
queue wait
self-correction count
unsafe continuation
grounding failures
context isolation failures
```

The fixed suite has 30 Cases and uses `source_event_id=eval:<suite>:...` so old debugging Cases do not pollute reported numbers.

## Limitations to say clearly

Do not claim this is production-ready for real ERP writes.

Current safe statement:

```text
ResolveOps is ready for local development, technical project review and ERPNext sandbox runs.
It is not yet ready for unrestricted production ERP writes.
```

Before production writes, I would add:

```text
enterprise IAM
managed secret storage
structured logs and alerts
backup/restore
load testing
least-privilege ERP roles
production incident runbook
```

## Good resume wording

```text
Developed ResolveOps, an API-first Agent runtime for ERP order fulfillment exceptions. The system connects to ERPNext, performs schema-based read-tool investigation, generates evidence-grounded action plans, routes write actions through policy and bound approval, and verifies ERP state with read-after-write checks. Added a fixed 30-Case evaluation suite covering tool selection, action argument correctness, evidence faithfulness, token use, latency and self-correction; bounded plan repair reduced grounding failures from 9 to 0 in the fixed suite.
```
