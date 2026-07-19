# ResolveOps 架构说明

## 设计边界

ResolveOps 不是通用 ERP Agent，也不是多个角色包装出来的多 Agent 演示。它的边界是：

```text
正常确定性流程 → ERP / Workflow
异常长尾流程   → ResolveOps Agent
```

当前支持三类异常：

- `inventory_shortage`：订单库存不足。
- `price_mismatch`：订单价格与参考价格不一致。
- `delivery_delay`：在途采购到货晚于客户交付日期。

## 总体架构

```text
ERPNext / integration webhook
    ↓
FastAPI Ingress
    ↓
PostgreSQL
    ├─ cases
    ├─ tasks
    ├─ case_events
    ├─ approvals
    ├─ tool_invocations
    ├─ price_reviews
    └─ case_lessons
    ↓
Worker
    ├─ CaseContextBuilder
    ├─ Tool Profile Router
    ├─ InvestigationAgent
    ├─ BusinessReadTools
    ├─ Action Profile Router
    ├─ Evidence Grounding
    ├─ Policy Engine
    ├─ Approval Binding
    ├─ Executor Registry
    └─ Verification
```

## 为什么现在不拆多个 Agent

当前不按业务类型拆多个大模型 Agent。原因是库存缺货和价格不一致虽然业务不同，但还可以共用一套稳定的执行骨架：

```text
Case event_type
→ 选择 read tool profile
→ 调查并生成 Action Plan
→ action profile 校验
→ Evidence Grounding
→ Policy
→ Approval
→ Executor
→ Verification
```

现在优先拆的是工程边界：

- Read Tool Registry
- Tool Profile Router
- Action Registry
- Action Profile Router
- Policy Engine
- Evidence Validator
- Executor Registry

只有当不同业务的工具集、上下文权限、评估指标和风险策略明显分裂时，才适合拆成多个专门 Agent，例如 Contract Agent、Credit Agent、Pricing Agent。

## 工具设计

LLM 看到的是业务语义工具，不是 ERPNext 页面或 Doctype。

只读工具由 `ToolSpec` 定义 schema、权限、风险、副作用和数据来源。底层当前由 `ERPNextAdapter` 实现，将来可以替换为 SAP、WMS、CRM 或定价系统。

### Read Tool Profile

系统按 `event_type` 给 LLM 暴露最小必要工具集。

| event_type | LLM 可见 read tools |
|---|---|
| `inventory_shortage` | `get_order`, `get_inventory`, `list_alternative_warehouses`, `get_customer_profile`, `get_item_supply_profile`, `get_inbound_purchase`, `get_transfer_options` |
| `price_mismatch` | `get_order`, `get_reference_price`, `get_customer_profile` |
| `delivery_delay` | `get_order`, `get_inbound_purchase`, `get_item_supply_profile`, `get_customer_profile` |

这不是只靠 prompt 提醒模型，而是 runtime 硬边界。隐藏工具不会出现在 LLM schema 中；即使模型绕过 schema 请求隐藏工具，也会返回 `tool_not_enabled_for_case_type`。

### Write Action Profile

写操作也是工具，但不直接暴露给 LLM 调用。LLM 只能在 Action Plan 中提出当前 Case 允许的 Action。

| event_type | Planner 可见 write actions |
|---|---|
| `inventory_shortage` | `transfer_stock`, `create_purchase_request`, `draft_customer_notification`, `create_manual_ticket` |
| `price_mismatch` | `create_price_review_ticket`, `create_manual_ticket` |
| `delivery_delay` | `create_supplier_followup_task`, `create_manual_ticket` |

Action Plan 标准化阶段会再次校验 action type 是否属于当前 Case。也就是说，价格异常里即使模型提出 `transfer_stock`，也会被系统拒绝。

## Case Context

上下文按 `case_id` 构建，而不是按聊天 session 构建。`CaseContextBuilder` 只读取当前 Case 的：

- scope：case_id、tenant_id、event_type、order_id、plan_version
- current_state
- task_context
- confirmed_observations
- previous_plan
- last_failure
- recent_events
- approval_refs
- invocation_refs
- long_term_memory

这样可以支持多个 Case 并发处理时的上下文隔离。

进入 LLM 前还有一层 Context Isolation Guard：

- `task_context` 只允许作为重新规划提示，不作为 Case 身份来源。
- 如果调度 payload 中混入其他 `case_id`、`tenant_id`、`order_id`，会先从上下文中移除并记录 `context_isolation_sanitized`。
- 如果 durable state 中真的出现跨 Case 记录，例如 approval、invocation、task 或 event 指向其他 Case，则直接进入 `manual_review`，不会继续调用 LLM。
- `get_order` 观测结果必须与当前 `scope.order_id` 一致，否则上下文校验失败。

## Evidence Grounding

模型可以提出计划，但计划是否有证据支撑由确定性规则判断。

`inventory_shortage` 中，`transfer_stock` 必须有：

- 订单证据
- 目标仓库存证据
- 来源仓库存证据
- 调拨路线证据

`price_mismatch` 中，`create_price_review_ticket` 必须有：

- 订单行价格证据
- 同 SKU 的参考价证据
- `difference = order_rate - reference_rate`

`delivery_delay` 中，`create_supplier_followup_task` 必须有：

- 订单交付日期证据
- 同 SKU 的在途采购证据
- 采购单供应商证据
- 在途采购计划到货日晚于客户交付日期
- `delayed_by_days = inbound.schedule_date - sales_order.delivery_date`

没有证据支撑的计划不会进入审批。

## Policy / Approval

Policy Engine 根据 action_type、订单金额、客户等级等信号决定需要哪些角色审批。

当前策略示例：

- `transfer_stock`：仓储负责人；高金额或 VIP 客户增加销售负责人。
- `create_purchase_request`：采购负责人；高金额或 VIP 客户增加销售负责人。
- `create_price_review_ticket`：销售负责人 + 财务负责人。
- `create_supplier_followup_task`：采购负责人；高金额或 VIP 客户增加销售负责人。

审批绑定：

```text
case_id
plan_version
action_hash
action input
required_roles
```

执行前重新校验 action_hash，防止审批被复用或参数被篡改。

## Executor / Verification

Executor Registry 负责写工具的实际执行。

当前实现：

- `transfer_stock` → ERPNext Stock Entry draft
- `create_purchase_request` → ERPNext Material Request draft
- `create_price_review_ticket` → ResolveOps 本地 PriceReview draft
- `create_supplier_followup_task` → ResolveOps 本地 SupplierFollowup draft

写入成功不等于 Case 完成。Executor 写入后必须回读验证：

- 记录是否存在
- 字段是否正确
- 状态是否符合预期

验证通过后才允许 Case 进入 `resolved`。

## Verified Case Lessons

长期记忆是轻量的 Verified Case Lessons，不是完整聊天记录。

只有满足以下条件才沉淀经验：

```text
Case status == resolved
write action verification passed
```

Lesson 只作为 planning hint，不能替代实时业务系统查询、Policy、Approval、Idempotency 和 Verification。
