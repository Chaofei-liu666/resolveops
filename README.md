# ResolveOps

ResolveOps 是一个面向企业业务异常的 Agent。它不处理确定性的正常流程，而是在业务进入异常分支后，自动调查原因、生成行动计划、申请审批、受控执行，并通过真实系统回读验证结果。

当前版本支持三类 Case：

- `inventory_shortage`：订单库存不足。Agent 调查库存、调拨路线、采购补货和客户约束，提出调拨或采购申请等 Action Plan。
- `price_mismatch`：订单价格与参考价格不一致。Agent 调查订单价格和参考价，创建受控的价格复核记录，不直接修改 ERP 价格。
- `delivery_delay`：在途采购到货晚于客户交付日期。Agent 调查订单、在途采购和客户约束，创建受控的供应商跟进记录，不直接修改 ERP 采购或销售日期。

## 核心闭环

```text
ERP 异常事件
→ 创建 Case
→ 按 event_type 暴露最小必要工具集
→ Agent 调用只读业务工具调查
→ Evidence-grounded Action Plan
→ Policy / Approval
→ Executor 执行写工具
→ Read-after-write Verification
→ resolved / replan / manual_review
```

## 当前已实现能力

- 真实 ERPNext API 接入，而不是 mock 系统。
- LLM 只允许调用只读业务工具，不能直接写 ERP。
- LLM 调用通过 LLMGateway 统一处理 provider timeout、错误包装、latency 和 usage telemetry。
- 只读工具通过 ReadToolScheduler 调度，支持批量并发、去重缓存和异常统一封装。
- 按 `event_type` 动态暴露 read tools，避免价格异常误调用库存/采购工具。
- 按 `event_type` 动态暴露 write Action schemas，避免 planner 提出不属于当前业务异常的行动。
- 写操作也是工具，但只能作为 Action Plan 被提出，由 Policy、Approval、Executor 控制执行。
- Operator 身份和角色从 `operators` 表按 API key hash 查询，审批不信任请求头自报角色。
- 审批绑定 `case_id + plan_version + action_hash`，防止参数篡改和审批重放；审批带 `expires_at`，支持撤销，Worker 执行前会再次校验审批生命周期。
- 写操作带 idempotency key，避免重复创建业务记录。
- PostgreSQL `FOR UPDATE SKIP LOCKED` 领取任务，支持多 Worker 并发。
- PostgreSQL advisory transaction lock 控制共享库存写入。
- SQL migration runner 按 `production/migrations/*.sql` 顺序应用版本化变更，并记录到 `schema_migrations`。
- ToolResult 统一表示工具成功、失败、可重试性和证据可用性。
- Case 详情返回确定性 `tool_trace`，展示每次工具调用的目的、参数、结果摘要、证据编号，以及它支撑了哪些 Action。
- CaseContextBuilder 按 `case_id` 构建上下文，并在进入 LLM 前清洗/校验 task context，避免多个 Case 串状态。
- Verified Case Lessons 只从 `resolved + verification passed` 的 Case 沉淀，并且只作为规划提示。
- 执行轨迹评估：Case Resolution、平均 read tool 次数、工具失败率、Verification Pass、Replan、Policy Denial、Handoff 等指标。

## 本地启动

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

填写 `.env`：

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
APPROVAL_TTL_SECONDS
```

启动服务：

```powershell
docker compose up -d --build
```

健康检查：

```powershell
Invoke-RestMethod http://localhost:8090/healthz
```

控制台：

```text
http://localhost:8090
```

## 测试

```powershell
python -m pytest -q
```

Docker 隔离测试入口：

```powershell
.\scripts\test.ps1
```

等价命令：

```powershell
docker compose --profile test run --rm test
```

当前容器化回归：

```text
64 passed, 1 skipped
```

## 文档

- [架构说明](docs/architecture.md)
- [运行手册](docs/runbook.md)
- [部署与上线安全清单](docs/deployment.md)
- [生产就绪度评估](docs/production-readiness.md)
- [面试问答笔记](docs/interview-notes.md)
- [可靠性评估与故障注入](docs/evals.md)

## 不提交的文件

`.env`、本地数据库、缓存目录不会进入 Git。敏感配置只保留在本地环境变量中，仓库只提交 `.env.example`。
