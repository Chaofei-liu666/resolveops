# CLI demo script

This script is written for a short local demo recording. It assumes ResolveOps is running against the local ERPNext sandbox.

Target length: 3 to 5 minutes.

## 0. Start from the launcher

On Windows, double-click:

```text
resolveops.cmd
```

Expected behavior:

```text
- checks local CLI config
- starts Docker services if Docker Desktop is ready
- waits for the ResolveOps API
- opens ResolveOps chat
```

If Docker Desktop is not running, start Docker Desktop first and reopen `resolveops.cmd`.

For a non-interactive screen recording, double-click:

```text
demo-record.cmd
```

It starts ResolveOps if needed, prints runtime status, lists recent Cases, prints the fixed evaluation summary, creates one demo Case and polls the Case trail. It does not approve write actions.

## 1. Show the top-level chat

Type:

```text
你好，你能做什么？
```

Point to explain:

```text
This is not bound to a Case yet. It is a no-tool operator chat.
It can answer normal questions, but it does not call ERP tools or write business data.
```

Useful follow-up:

```text
你的底层模型是什么？
```

Expected behavior:

```text
ResolveOps answers from server-side LLM_MODEL / LLM_BASE_URL config,
without printing the API key.
```

## 2. Create or list Cases

Inside chat:

```text
/cases
```

If there is no fresh Case, create one:

```text
/new
```

Recommended demo input:

```text
type: inventory_shortage
order: SAL-ORD-2026-00002
reason: CLI demo inventory shortage
```

Point to explain:

```text
A Case is the durable business unit.
The chat session is not the source of truth.
```

## 3. Watch the Agent trail

Use the Case ID from the output:

```powershell
python resolveops.py case watch <case-id>
```

Expected events to point out:

```text
context_built
tool_scheduled
tool_observation
evidence_grounding_passed
agent_plan_created
approval_requested / waiting_approval
```

What to say:

```text
The Agent calls read tools to collect ERP evidence, then proposes an action plan.
It does not directly execute writes just because the model suggested an action.
```

## 4. Show Case details

```powershell
python resolveops.py case show <case-id>
```

Point to explain:

```text
The output shows the selected action, rationale, required approval roles,
tool trace and recent events.
```

## 5. Approve controlled execution

Copy the approval ID from `case show`, then approve with the required operator key.

Example:

```powershell
python resolveops.py --operator-key <warehouse-manager-key> approval approve <approval-id>
python resolveops.py --operator-key <sales-manager-key> approval approve <approval-id>
```

Point to explain:

```text
Approval is bound to the Case, plan version and action hash.
It is not a broad permission to call any ERP write API.
```

After approval:

```powershell
python resolveops.py case watch <case-id>
python resolveops.py case show <case-id>
```

Expected final path:

```text
execution_started
verification_passed
resolved
```

## 6. Show evaluation

```powershell
python resolveops.py eval summary --suite core-v4 --limit 50
```

Point to explain:

```text
The project does not only test final status.
It also checks tool choice, action argument correctness, evidence faithfulness,
token use, latency, self-correction and unsafe continuation.
```

## 7. Optional fault injection

Only run this in local or staging sandbox:

```powershell
python resolveops.py fi run inventory_changed_before_execution `
  --case <case-id> `
  --item SKU-A12 `
  --warehouse "重庆仓 - ROPS" `
  --new-qty 0 `
  --reason "demo: source inventory changed before execution"
```

Point to explain:

```text
Fault injection changes ERPNext sandbox data through ResolveOps API.
The CLI never bypasses the server-side policy layer.
```

## Short narration

```text
ResolveOps handles order fulfillment exceptions as durable Cases.
The model can investigate and propose actions, but it cannot directly authorize itself to write ERP data.
Read tools are schema-defined and selected by Case type.
Write operations go through evidence grounding, policy checks, bound approvals, idempotent execution and read-after-write verification.
The evaluation suite checks not only whether a Case reaches a final status, but also whether the Agent chose the right tools, used grounded action arguments, stayed within budget, and stopped safely when needed.
```
