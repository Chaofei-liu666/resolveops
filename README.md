# ResolveOps

ResolveOps 是一个面向企业订单履约异常的 Agent。它不处理确定性正常流程，而是在订单进入异常分支后，自动调查原因、生成行动计划、申请审批、受控执行，并通过真实业务系统回读验证结果。

当前第一版聚焦 ERP 订单履约异常，尤其是库存不足场景。项目使用 ERPNext 作为真实业务系统，PostgreSQL 保存 Case、事件、审批、工具调用、验证结果和轻量长期记忆。

## 核心闭环

```text
ERP 异常事件
→ 创建 Case
→ Agent 调用只读工具调查
→ Evidence-grounded Action Plan
→ Policy / Approval
→ Executor 执行写工具
→ Read-after-write Verification
→ resolved / replan / manual_review
```

## 已实现能力

- 真实 ERPNext API 接入，而不是 mock 系统。
- LLM 只允许调用只读业务工具，不能直接写 ERP。
- 写操作以 Action Plan 形式提出，由 Policy、Approval、Executor 控制。
- 审批绑定 `case_id + plan_version + action_hash`，防止参数篡改和审批重放。
- 写操作带 idempotency key，避免重复创建业务单据。
- PostgreSQL `FOR UPDATE SKIP LOCKED` 领取任务，支持多 Worker 安全并发。
- PostgreSQL advisory transaction lock 控制共享库存写入。
- ToolResult 统一表示工具成功、失败、是否可重试、是否可作为证据。
- CaseContextBuilder 按 `case_id` 构建上下文，避免多 Case 串状态。
- Verified Case Lessons：只有 resolved 且验证通过的 Case 才沉淀经验，且只作为规划提示。
- 执行轨迹评估：Case Resolution、Verification Pass、Replan、Policy Denial、Handoff 等指标。

## 本地启动

先复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

然后填写 `.env`：

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
..\agent-sre\.venv\Scripts\python.exe -m pytest tests -q
```

当前本地测试应通过：

```text
37 passed, 1 skipped
```

## 文档

- [架构说明](docs/architecture.md)
- [运行手册](docs/runbook.md)
- [面试问答笔记](docs/interview-notes.md)
- [可靠性评估与故障注入](docs/evals.md)

## 不提交的文件

`.env`、本地数据库、缓存目录不会进入 Git。敏感配置只保留在本地环境变量中，仓库只提交 `.env.example`。

