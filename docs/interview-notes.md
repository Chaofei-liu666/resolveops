# ResolveOps 面试问答笔记

## 1. 高并发下 Agent 状态怎么持久化？

我的设计不是按聊天 session 管理，而是按业务 `case_id` 管理。核心状态放 PostgreSQL，不放 Redis。

原因是 Case、审批、工具调用、验证结果都需要事务、审计和恢复。Redis 可以用于限流、缓存和短生命周期协调，但不适合作为业务状态唯一来源。

当前实现：

```text
cases
tasks
case_events
approvals
tool_invocations
case_lessons
```

并发控制：

```text
任务领取：SELECT ... FOR UPDATE SKIP LOCKED
写操作幂等：idempotency_key 唯一约束
共享库存写入：PostgreSQL advisory transaction lock
上下文隔离：CaseContextBuilder 只按 case_id 取状态
```

原子性通过数据库事务保证。审批执行时校验 `case_id + plan_version + action_hash`，防止旧审批或跨 Case 审批被复用。

## 2. Redis 还是数据库？

核心业务状态进数据库。

Redis 更适合：

- API 限流
- 短期缓存
- 临时分布式锁
- 实时进度缓存

但审批、计划、事件、工具调用、验证结果必须进数据库，因为这些需要审计和恢复。

## 3. Plan-and-Solve 和 ReAct 区别？

ResolveOps 是混合式：

```text
调查阶段：类似 ReAct，只允许调用只读工具
规划阶段：Plan-and-Solve，生成 Action Plan
执行阶段：确定性 Executor
```

ReAct 更适合开放探索；企业写操作不能让模型在循环里直接执行，所以模型只能提出计划。

执行流程：

```text
read tools
→ observations
→ Action Plan
→ Evidence Grounding
→ Policy
→ Approval
→ Executor
→ Verification
```

如果 preflight 发现库存变化，旧审批失效，Case 进入 replanning。

## 4. 多工具并行调用怎么做？

当前版本没有做 aggressive parallel tool calling。读工具目前按模型回合顺序执行，写工具必须串行。

原因是第一版优先保证证据链和写操作安全。

如果后续实现并行，只并发无副作用读工具：

```text
ToolScheduler
→ 判断依赖和副作用
→ asyncio.gather 调多个 HTTP API
→ 每个结果包装成 ToolResult
→ 汇总 observations
→ 一次性给 Planner
```

写工具不并行。同一业务对象写入必须串行或加锁。

## 5. 长上下文爆炸怎么处理？

本项目不是保存完整聊天历史，而是构建结构化 Case Context。

当前上下文包含：

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

最近事件有窗口限制，长期经验沉淀为 Verified Case Lessons。

关键点：

```text
压缩的是执行上下文，不是简单总结聊天记录。
完整审计仍保留在数据库 event log。
```

## 6. 什么时候拆多个 Agent？

不按工具数量机械拆分。拆分边界看业务职责、上下文、权限和评估指标是否不同。

适合拆：

- 订单异常处置
- 合同审查
- 客户沟通
- 采购寻源

不适合拆：

- 库存查询 Agent
- 订单查询 Agent
- 审批 Agent

当前版本没有拆多 Agent，因为一个主 Agent + Tool Registry + Executor Registry 更清晰。

## 7. 语义路由怎么做？

当前入口是 ERP webhook，事件类型明确，所以用规则路由即可。

语义路由适合自然语言、邮件、工单入口。后续可加小模型输出：

```text
intent
risk_level
confidence
target_agent_or_tool_domain
```

低置信度不直接执行，转人工或只允许只读调查。

## 8. 怎么评估 Agent 执行轨迹？

不只评估最终文本，而是评估事件轨迹。

指标：

- Case Resolution Rate
- Verification Pass Rate
- Policy Denial Count
- Evidence Grounding Failure Count
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

## 9. LLM API 高并发限流怎么做？

当前版本做了运行预算：

- 最大调查轮次
- 最大 read tool 次数
- 最大 replanning 次数
- HTTP timeout

还没有完整 ProviderGateway。

生产下一步会加：

```text
LLMGateway
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

获取失败直接 `resource_busy`，不长时间等待。事务结束自动释放，减少锁泄漏风险。

Redis setnx 适合高频短锁，但要处理 TTL、续租、误释放和主从一致性。当前业务状态已在 PostgreSQL，用 advisory lock 更简单可靠。

## 11. 记忆为什么不用 Mem0？

不是 Mem0 没价值，而是当前项目更适合轻量 Verified Case Lessons。

规则：

```text
只有 resolved 且 verification passed 的 Case 才能沉淀 lesson
lesson 只作为 planning hint
不能替代 ERP 实时查询、Policy、Approval、Verification
```

这个设计更能体现企业 Agent 的记忆边界。

