# ResolveOps reliability evaluation plan

## Unit checks

Run on every change:

```powershell
python -m pytest -q
```

Current local regression:

```text
41 passed, 1 skipped
```

| Risk | Test | Expected safe outcome |
|---|---|---|
| Illegal model plan | Missing action arguments / unregistered action | No Action Plan is created |
| Scope escape | Inventory request for an unauthorized warehouse | Tool call denied |
| High-risk case | Order value exceeds 100,000 | Extra approval roles required |
| Invalid model response | Non-JSON planning response | Handoff, never write |
| Budget exhaustion | Agent read-tool budget is exhausted | Recorded in `missing_information`; plan uses only collected evidence |
| Ungrounded model plan | Action lacks required tool evidence | `evidence_grounding_failed`; no approval or write |
| Evaluation semantics | Case with write invocation has verification event | `verification_complete=true` |

## Runtime evaluation API

ResolveOps derives evaluation metrics from the actual Case execution trail instead of maintaining a separate mock score table.

Endpoint:

```text
GET /v1/evals/summary?limit=50
Required role: ops_admin or config_admin
```

Computed signals:

| Metric | Source of truth |
|---|---|
| Case Resolution Rate | `cases.status == resolved` |
| Verification Pass Rate | `tool_invocations` plus `verification_passed` / `verification_failed` events |
| Recovery Count | `replan_requested`, `task_requeued`, `manual_review_required` events |
| Policy Denial Count | `policy_denied` events |
| Evidence Grounding Failure Count | `evidence_grounding_failed` events |
| Manual Handoff Count | `handoff`, `manual_review_required` events |
| Task Failure Count | failed `tasks` |

Historical fault-injection cases intentionally stop at manual review, so resolution rate is not the only quality signal for this mixed dataset. The stronger signal is that all write cases must pass independent verification or stop safely.

## Evidence grounding gate

Before an Agent plan can create approvals, ResolveOps validates that every executable action is supported by read-tool evidence.

Current checks:

| Action | Required evidence |
|---|---|
| `transfer_stock` | Sales Order, target inventory, source inventory with enough usable stock, configured transfer lane |
| `create_purchase_request` | Sales Order, target inventory, item supply profile with lead time, inbound purchase check |
| `create_price_review_ticket` | Sales Order item rate, same-SKU reference price, non-zero price difference |
| multi-action plan | Combined action quantity must cover the computed shortage |

This is intentionally deterministic. The LLM proposes actions; the system verifies the Evidence-to-Action link before approval.

## Integration / fault injection checks

Run in the isolated ERPNext test tenant.

| ID | Injected condition | Expected result |
|---|---|---|
| FI-01 | Same webhook delivered twice | One Case and one investigate Task |
| FI-02 | Two Cases reserve the same source warehouse/SKU | One write at a time; no oversell |
| FI-03 | Source inventory changes after approval | Approval invalidated; Agent replans |
| FI-04 | Worker stops during read-only investigation | Task is safely requeued |
| FI-05 | Worker stops during possible write | Manual review; no blind retry |
| FI-06 | One role approves a dual-role request | Approval remains pending; no execute Task |
| FI-07 | Action parameters change after approval | Action hash mismatch; execution denied |
| FI-08 | Source inventory changes after one action in a multi-action plan is approved | Approved action is invalidated before write; no Invocation; Agent replans the whole Case |
| AI-01 | Customer does not allow partial delivery | Agent may still propose internal split fulfillment only if all actions complete before delivery date; it must not justify customer partial delivery |

Record each integration run with: Case ID, injected condition, event trail, business document ID if any, and final Case state.

## Executed runs

| ID | Case | Result |
|---|---|---|
| FI-01 | `b2cb3317-b88e-43d2-8e70-6f05d26009c1` | Duplicate webhook returned the same Case with `duplicate=true`; exactly one investigation Task was created; no write occurred. |
| FI-02 | `fi02-8af44bff-beb0-4fd0-9add-27edc30cd4a1`, `fi02-f2a867bc-6b52-4fce-9e14-ed1c019f09b9` | Two approved Actions concurrently targeted the same source warehouse/SKU. PostgreSQL advisory locking allowed one verified draft and blocked the other before any Invocation or ERP write. |
| FI-03 | `b2cb3317-b88e-43d2-8e70-6f05d26009c1` | Source stock was changed after approval. The old approval was invalidated before ERP write, and the Agent re-investigated instead of blindly executing. |
| FI-04 | `fi04-8de7953a-fc0f-4cc2-aa9e-03344d31b28a` | A read-only investigation Task was seeded with an expired Worker lease. Recovery emitted `task_requeued`, then the Task completed on its second attempt with zero write invocations. |
| FI-05 | `fi05-cdbe9465-9ca8-4311-bae6-3ff5c12fefeb` | A possible-write execute Task was seeded with an expired Worker lease. Recovery marked the Task `failed`, set the Case to `manual_review`, emitted `manual_review_required`, and performed no retry. |
| FI-06 | `b2cb3317-b88e-43d2-8e70-6f05d26009c1` | The first approval emitted `approval_partial` and left the action blocked. Only the second required approval created the execute Task. |
| FI-07 | `fi07-0c2257f9-aa9c-44cf-a8b8-502cdda62352` | An approval bound to transfer quantity 30 was executed against a tampered current plan with quantity 31. The action-hash check stopped execution, placed the Case in `manual_review`, and created zero write invocations. |
| FI-08 | `b7f61c90-b9d7-4aa0-9252-7db66c24c8d8` | A multi-action plan was approved, then source inventory was changed before write. The Worker re-read source inventory, invalidated the approval, created no ERP Invocation, and queued reinvestigation. |
| AI-01 | `89fdc843-68ca-4c19-b3a3-e6604cc1944f` | Customer `allows_partial_delivery=false` was read from ERPNext. The Agent still proposed internal transfer + purchase because both actions complete before the delivery date, and its rationale did not rely on customer partial delivery. No write was approved during this evaluation run. |
| ENV-01 | `4ac6e175-88cc-4e97-82db-f602da4fec66` | Real webhook regression was triggered while ERPNext was unavailable from the Worker container. The Agent preserved the failure as `ToolResult(status=failed, retryable=true, evidence_usable=false)`, produced no Action Plan, created no approval, and stopped at `manual_review`. |
| E2E-01 | `4fb78244-ad8b-4690-bd7f-226f9c782833` | Real `inventory_shortage` run after restoring ERPNext services and preparing test stock with `MAT-RECO-2026-00004`. The Agent gathered read-tool evidence, proposed `transfer_stock`, required warehouse + sales approvals for the high-value order, created verified draft transfer `MAT-STE-2026-00007`, closed the Case as `resolved`, and emitted `lessons_recorded` with three Verified Case Lessons. |
| MEM-01 | `fe692874-cada-4b87-a0a7-ac2f0ecb6dda` | A follow-up Case for the same order confirmed CaseContextBuilder retrieved active same-tenant Verified Case Lessons. The new Case reached `waiting_approval` with `memory_count=1`, including the operational lesson from `E2E-01`. No second ERP write was approved during this memory check. |
| E2E-02 | `c51cbdd5-1f70-4221-869d-a844924f6af6` | Real `price_mismatch` run. ERPNext `Item Price` access initially failed with 403, then the integration user was granted `Sales Master Manager` to read reference price. The Agent observed Sales Order rate 5000 and reference price 4500 for `SKU-A12`, proposed `create_price_review_ticket`, required sales + finance approvals, created verified local PriceReview `bb56dcbf-1f11-4b64-83ab-57d4b693bbb3`, closed the Case as `resolved`, and recorded Verified Case Lessons. |
| E2E-03 | `f8b17e82-e7a8-4e70-ba42-c595992fecee` | Real `price_mismatch` run after introducing dynamic Tool/Action Profiles. The LLM-visible read tools were limited to `get_order`, `get_reference_price`, and `get_customer_profile`; no inventory, transfer, inbound purchase, or supply-profile tools appeared in the observed trajectory. The planner proposed only `create_price_review_ticket`, dual approval passed, local PriceReview `ff6f3a8d-57d0-4e6a-ad57-e1dda20f7c48` was verified, and the Case closed as `resolved`. |
