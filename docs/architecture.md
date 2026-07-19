# ResolveOps 架构说明

## 设计边界

ResolveOps 不是通用 ERP Agent，也不是多 Agent 角色扮演系统。它的边界是：

```text
正常确定性流程 → 交给 ERP / Workflow
异常长尾流程 → 交给 ResolveOps Agent
```

第一版聚焦订单履约异常，尤其是库存不足导致无法按期交付的 Case。

## 总体架构

```text
ERPNext Webhook
    ↓
FastAPI Ingress
    ↓
PostgreSQL
    ├─ cases
    ├─ tasks
    ├─ case_events
    ├─ approvals
    ├─ tool_invocations
    └─ case_lessons
    ↓
Worker
    ├─ CaseContextBuilder
    ├─ InvestigationAgent
    ├─ BusinessReadTools
    ├─ Evidence Grounding
    ├─ Policy Engine
    ├─ Approval Binding
    ├─ Executor Registry
    └─ Verification
```

## Agent 运行流程

### 1. 创建 Case

ERPNext 或集成层发送 `inventory_shortage` webhook。API 校验 HMAC 签名、`event_id`、`tenant_id` 和 `order_id`，然后创建 Case 和 investigation Task。

`event_id` 是输入侧幂等边界，重复 webhook 只返回同一个 Case。

### 2. 构建 Case Context

Worker 开始调查前调用 `CaseContextBuilder(case_id)`。

上下文只包含当前 Case 的：

- Case 状态
- 最近事件
- 已确认工具观察
- 审批引用
- 工具调用引用
- 任务引用
- 上次失败原因
- 同租户相关 Verified Case Lessons

这解决多 Case 并发时的上下文隔离问题。

### 3. 调查阶段

LLM 只能看到只读工具，例如：

- `get_order`
- `get_inventory`
- `get_customer_profile`
- `get_item_supply_profile`
- `get_inbound_purchase`
- `get_transfer_options`

工具失败会包装为 `ToolResult`，例如：

```json
{
  "status": "failed",
  "error_code": "tool_execution_failed",
  "retryable": true,
  "evidence_usable": false
}
```

失败事实只能表示 unknown，不能被模型解释成“没有库存”或“没有采购”。

### 4. 计划阶段

LLM 不能直接写 ERP，只能提出 Action Plan：

- `transfer_stock`
- `create_purchase_request`
- `draft_customer_notification`
- `create_manual_ticket`

Action Plan 会被标准化为受控 action envelope，包含：

- action_id
- action_type
- input
- risk
- execution
- verification
- compensation
- tool metadata

### 5. Evidence Grounding

系统用确定性规则检查模型计划是否有证据支撑。

例如 `transfer_stock` 必须有：

- 订单证据
- 目标仓库存证据
- 来源仓库存证据
- 物流路线证据

没有证据支撑的计划不会进入审批。

### 6. Policy / Approval

Policy Engine 根据订单金额、客户等级和 action_type 决定所需角色。

审批绑定：

```text
case_id
plan_version
action_hash
action input
required_roles
```

执行前会重新校验 action_hash，防止审批被复用或参数被篡改。

### 7. Executor

Worker 不直接写 ERP，而是通过 Executor Registry。

目前实现：

- `transfer_stock` → ERPNext Stock Entry draft
- `create_purchase_request` → ERPNext Material Request draft

写操作前执行 preflight，例如重新读取来源库存。如果库存变化，旧审批失效，Case 进入 replanning。

### 8. Verification

ERPNext 返回成功不等于任务完成。Executor 写入后必须回读源系统验证：

- 单据是否存在
- 数量是否正确
- 仓库是否正确
- 状态是否符合预期

验证通过才允许 Case 关闭。

### 9. Verified Case Lessons

只有满足以下条件才生成长期记忆：

```text
Case status == resolved
write action verification passed
```

Lesson 只作为 planning hint，不能替代：

- ERP 实时查询
- Policy
- Approval
- Idempotency
- Verification

## 为什么没有直接使用 Mem0 / 向量库 / 知识图谱

当前核心数据主要是结构化业务状态，适合数据库、API 和确定性规则。向量库、Mem0、知识图谱适合后续大量非结构化制度、邮件、合同进入系统后再引入。

本项目第一版优先证明：

```text
Agent 如何在真实企业系统里安全、可靠、可验证地完成一个业务 Case。
```

