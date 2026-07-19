# ResolveOps 面试问答笔记

## 1. 高并发下 Agent 状态怎么持久化？

项目不按聊天 session 管理状态，而是按业务 `case_id` 管理。核心状态放 PostgreSQL，不放 Redis。

原因是 Case、审批、计划、工具调用、验证结果都需要事务、审计和恢复。Redis 可以做限流、短期缓存和临时协调，但不适合作为业务状态唯一来源。

当前实现：

```text
cases
tasks
case_events
approvals
tool_invocations
price_reviews
case_lessons
```

并发控制：

```text
任务领取：SELECT ... FOR UPDATE SKIP LOCKED
写操作幂等：idempotency_key 唯一约束
共享库存写入：PostgreSQL advisory transaction lock
上下文隔离：CaseContextBuilder 只按 case_id 取状态，进入 LLM 前清洗 task_context 并校验 durable refs
```

## 2. Redis 还是数据库？

核心业务状态进数据库。

Redis 更适合：

- API 限流
- 临时缓存
- 短生命周期分布式锁
- 实时进度缓存

但审批、计划、工具调用、验证结果必须进数据库，因为这些数据需要恢复和审计。

## 2.1 Operator 身份和权限怎么做？

当前本地版本不是完整 SSO，但已经不信任调用方自报角色。

```text
X-Operator-Key
→ sha256
→ operators.api_key_hash
→ subject / role / tenant_id
```

审批时使用数据库中的 `role` 判断是否满足 required_roles。`X-Operator-Role` 即使被伪造为 `ops_admin`，也不会影响授权结果。生产环境应把这层替换成 SSO / IAM / API Gateway 写入的身份上下文，或由管理员流程管理 operators 表。

## 3. Plan-and-Solve 和 ReAct 的区别？

ResolveOps 是混合式：

```text
调查阶段：类似 ReAct，只允许调用只读工具
规划阶段：Plan-and-Solve，生成 Action Plan
执行阶段：确定性 Executor
```

ReAct 适合开放探索，但企业写操作不能让模型边想边直接执行。所以模型只负责调查和提出计划，真正执行前还要经过 Evidence Grounding、Policy、Approval 和 Verification。

## 4. 多工具并行调用怎么做？

当前版本做的是 read-only Tool Scheduler，不做写工具并发。

```text
LLM 返回一批 tool_calls
→ ReadToolScheduler 过滤重复调用
→ ThreadPoolExecutor 并发执行同步 HTTP read tools
→ 每个结果包装成 ToolResult
→ 按原 tool_call 顺序汇总 observations
→ 返回给模型，并最终交给 Planner
```

为什么用线程池，不用协程：当前 ERPNextAdapter 是同步 `httpx.get/post`，用线程池改动最小。如果以后 Adapter 改成 `httpx.AsyncClient`，Scheduler 可以替换成 `asyncio.gather`。

异常兜底：

- 单个工具异常会变成 `ToolResult(status=failed, error_code=tool_scheduler_failed)`；
- 工具错误只代表事实未知，不代表业务事实不存在；
- 重复工具调用复用缓存，避免模型反复读同一事实；
- 写工具不并发，同一业务对象写入必须串行或加锁。

## 5. 长上下文爆了怎么办？

项目不保存完整聊天历史，而是构建结构化 Case Context。

当前上下文包括：

- scope
- current_state
- task_context
- confirmed_observations
- previous_plan
- last_failure
- recent_events
- approval_refs
- invocation_refs
- long_term_memory

完整审计仍保留在数据库 event log 中；给模型的是压缩后的执行上下文。

为了避免多 Case 并发时串上下文，当前还做了 Context Isolation Guard：

- scheduler 传入的 `task_context` 不是身份来源，foreign `case_id / tenant_id / order_id` 会被移除；
- approvals、tool_invocations、tasks、events 必须都属于当前 `case_id`；
- Verified Case Lessons 只能来自同一 `tenant_id`；
- 如果校验失败，Case 进入 `manual_review`，不会继续让模型规划。

## 6. 什么时候拆多个 Agent？

不因为新增业务就立刻拆 Agent。拆分边界看四件事：

1. 工具集是否明显不同。
2. 上下文和权限边界是否不同。
3. 风险策略是否不同。
4. 单个 Agent 是否因为工具太多开始选错工具。

当前阶段采用：

```text
一个主 Agent
多 event_type
多 Read Tool
多 Action
统一 Policy / Executor / Verification
```

以后如果加入合同审查、客户信用、法务条款，才适合拆成 Contract Agent、Credit Agent、Pricing Agent。

## 7. 语义路由怎么做？

当前入口是 ERP webhook，事件类型明确，所以用规则路由：

```text
inventory_shortage → 库存调查工具和调拨/采购 Action
price_mismatch → 价格调查工具和价格复核 Action
```

如果未来入口变成自然语言、邮件或工单，可以加小模型分类：

```text
intent
risk_level
confidence
target_domain
```

低置信度不直接执行，只进入只读调查或人工分流。

## 8. 怎么评估 Agent 执行轨迹？

不只评估最终文本，而是评估事件轨迹：

- Case Resolution Rate
- Average Read Tool Calls
- Tool Failure Rate
- Verification Pass Rate
- Policy Denial Count
- Approval Waiting Cases
- Evidence Grounding Pass Count
- Evidence Grounding Failure Count
- Context Isolation Sanitized / Failed Count
- Replan Count
- Manual Handoff Count
- Task Failure Count

这些来自真实表：

```text
cases
case_events
approvals
tool_invocations
tasks
```

每个 Case 还会输出 `stage_sequence`，用于看清楚执行链路走到了哪一步，例如：

```text
context_built
→ tool_scheduled
→ tool_observation
→ evidence_grounding_passed
→ agent_plan_created
→ approval_granted
→ execution_started
→ verification_passed
```

## 9. LLM API 高并发限流怎么处理？

当前版本有运行预算：

- 最大调查轮次
- 最大 read tool 次数
- 最大 replanning 次数
- HTTP timeout

生产下一步可以加 `LLMGateway`：

```text
per-provider max_concurrency
token bucket
request queue
exponential backoff
circuit breaker
usage logging
fallback model
```

## 10. 分布式锁怎么设计？

当前使用 PostgreSQL advisory transaction lock。

锁粒度：

```text
inventory:{source_warehouse}:{sku}
```

获取失败直接返回 `resource_busy`，不长时间等待。事务结束自动释放，减少锁泄漏风险。

Redis setnx 也能做锁，但要处理 TTL、续租、误释放和主从一致性。当前业务状态已经在 PostgreSQL，用 advisory lock 更简单可靠。

## 11. 记忆为什么不用 Mem0？

不是 Mem0 没价值，而是当前项目更适合轻量 Verified Case Lessons。

规则：

```text
只有 resolved 且 verification passed 的 Case 才能沉淀 lesson
lesson 只作为 planning hint
不能替代 ERP 实时查询、Policy、Approval、Verification
```

这比直接接入向量库或 Mem0 更能体现企业 Agent 的记忆边界。

## 12. 工具是不是 ERP 功能？

不是。工具是 LLM 连接外部世界的业务能力接口。ERPNext 只是当前适配器。

例如：

```text
get_reference_price
```

对 LLM 来说是“读取参考销售价”。底层今天可以查 ERPNext Item Price，未来也可以换成定价系统、CRM 或合同系统。

写工具同理：

```text
create_price_review_ticket
```

它不是 ERP 页面按钮，而是一个受治理的业务动作：有 schema、权限、风险、副作用、幂等和验证。
