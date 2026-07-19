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
    ├─ operators
    ├─ tool_invocations
    ├─ schema_migrations
    ├─ price_reviews
    └─ case_lessons
    ↓
Worker
    ├─ Migration Runner
    ├─ CaseContextBuilder
    ├─ Tool Profile Router
    ├─ LLMGateway
    ├─ InvestigationAgent
    ├─ BusinessReadTools
    ├─ Action Profile Router
    ├─ Evidence Grounding
    ├─ Policy Engine
    ├─ Approval Binding
    ├─ Executor Registry
    └─ Verification
```

## Schema Migration

ResolveOps 现在使用轻量 SQL migration runner 管理 schema 演进。

```text
production/migrations/*.sql
→ 按文件名版本顺序执行
→ 写入 schema_migrations(version, filename, checksum, applied_at)
```

本地开发启动时仍会用 SQLAlchemy metadata 创建空库基础表，但字段演进、索引和在线升级逻辑必须进入版本化 SQL 文件。API 和 Worker 启动时都会调用同一套 runner，并用 PostgreSQL advisory lock 防止两个进程并发执行 migration。

生产部署时建议在 CI/CD 中用独立 migration 用户执行这些 SQL，然后移除 API / Worker 数据库账号的 DDL 权限。

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

## LLM Gateway

Agent 不直接调用 LLM Provider HTTP API，而是通过 `LLMGateway`。

当前 Gateway 负责：

- 统一拼接 chat-completions endpoint。
- 注入当前模型名。
- 设置 provider timeout。
- 将 timeout、HTTP error、provider error 规范化成 `LLMResult`。
- 标记错误是否可重试。
- 记录 model、latency_ms、usage。
- 让 Agent 在 LLM 调用失败时安全 handoff，不继续生成写计划。

后续如果需要生产级流控，可以在这个边界继续加入：

```text
per-provider max_concurrency
token bucket
request queue
exponential backoff
circuit breaker
fallback model
```

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

## Read Tool Scheduler

调查阶段的工具调用由 `ReadToolScheduler` 执行，而不是让 Agent 直接散落调用外部系统。

当前调度边界：

- 只调度 read tools，不调度写工具。
- 同一批相同的工具名 + 参数只执行一次，其余调用复用结果。
- 已经执行过的相同调用会从 `seen` 缓存返回，避免重复读取。
- 使用 `ThreadPoolExecutor` 并发执行同步 HTTP read tools，因为当前 ERPNext Adapter 是同步 `httpx` 调用。
- 每个工具结果统一封装成 `ToolResult`，异常不会直接炸穿 Agent loop。
- 返回给 LLM 的每个 tool_call 仍然有独立 tool response，保证 Chat Completions 协议完整。

写操作不进入 Scheduler。写操作必须走：

```text
Action Plan
→ Evidence Grounding
→ Policy
→ Approval
→ Executor
→ Verification
```

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

Operator 身份从 `operators` 表读取：

```text
X-Operator-Key
→ sha256
→ operators.api_key_hash
→ subject / role / tenant_id
```

API 路由不再信任调用方传入的 `X-Operator-Role`。本地开发可以通过 `OPERATOR_SEED_KEYS` seed 测试 operator；生产环境应由 SSO / IAM / API Gateway 或管理员流程写入 operators 表。

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
expires_at
```

执行前重新校验 action_hash、审批状态和 expires_at，防止审批被复用、参数被篡改或旧审批长期有效。审批也可以被撤销；撤销后 Case 进入 manual_review，Worker 不会继续执行残留的 execute task。

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

## Runtime Evals

ResolveOps 不用“答案相似度”评估 Agent，而是从真实 Case 事件轨迹计算运行指标。

当前每个 Case 会提取：

- `stage_sequence`：关键阶段顺序，例如 `context_built → tool_scheduled → tool_observation → evidence_grounding_passed → agent_plan_created → approval_granted → execution_started → verification_passed`。
- `tool_call_count`：read tool 调用次数。
- `tool_failure_count`：失败或不可用的 read tool 结果。
- `tool_scheduler_sources`：工具结果来自 executed、cache、deduped 还是 unknown。
- `pending_approval_count`：是否卡在审批。
- `verification_complete`：写入是否完成独立回读验证。
- `blocked_event_count`：是否因证据不足、策略拒绝、上下文隔离失败、验证失败或人工接管而停止。

汇总接口 `/v1/evals/summary` 再计算：

- Case Resolution Rate
- Average Read Tool Calls
- Tool Failure Rate
- Verification Pass Rate
- Approval Waiting Cases
- Evidence Grounding Pass / Failure
- Context Isolation Sanitized / Failed
- Replanned Cases
- Manual Handoff Cases
