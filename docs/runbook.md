# ResolveOps 运行手册

## 1. 前置条件

需要本机具备：

- Docker Desktop
- 可访问 ERPNext 的 Docker 网络
- `.env` 中配置 ERPNext API、LLM API、Webhook Secret、Operator Key

检查当前目录：

```powershell
cd E:\iwen-codex\工作简历\resolveops
```

## 2. 启动服务

```powershell
docker compose up -d --build
```

检查容器：

```powershell
docker compose ps
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Networks}}"
```

健康检查：

```powershell
Invoke-RestMethod http://localhost:8090/healthz
```

## 3. ERPNext 连接检查

ResolveOps API 和 Worker 必须和 ERPNext 在同一个 Docker 网络。

检查网络：

```powershell
docker network inspect frappe_docker_frappe_network
```

如果 Agent 工具返回：

```text
tool_execution_failed / ConnectError
```

优先检查：

- ERPNext frontend/backend 是否正在运行
- `.env` 中 `ERPNEXT_BASE_URL` 是否能从容器内访问
- ResolveOps 是否加入了 `frappe_docker_frappe_network`
- ERPNext 容器是否在重启

## 4. 触发 Webhook

Webhook body 示例：

```json
{
  "event": "inventory_shortage",
  "tenant_id": "demo",
  "event_id": "unique-event-id",
  "order_id": "SAL-ORD-2026-00002"
}
```

Header 需要：

```text
X-ResolveOps-Signature: sha256=<HMAC_SHA256(body, WEBHOOK_SECRET)>
```

重复发送相同 `event_id` 应该返回同一个 Case。

## 5. 审批

审批接口：

```text
POST /v1/approvals/{approval_id}/approve
POST /v1/approvals/{approval_id}/revoke
```

需要 Header：

```text
X-Operator-Key: <OPERATOR_API_KEY>
```

Operator 的 `subject / role / tenant_id` 从数据库 `operators` 表按 API key hash 查询。`X-Operator-Role` 不再作为授权来源；即使调用方伪造该 header，也不会改变后端识别出的角色。

本地开发可通过 `.env` 的 `OPERATOR_SEED_KEYS` seed 多个测试 operator：

```text
subject:role:key;subject:role:key
```

高金额或 VIP Case 可能需要多个数据库角色分别审批。

审批不是永久授权。每条审批会带 `expires_at`，默认由 `APPROVAL_TTL_SECONDS` 控制；过期审批不能继续批准，已经批准但尚未执行的审批在 Worker 执行前也会被重新校验。需要人工停止时，调用 revoke 接口会把 Case 转入 `manual_review`，即使队列里还残留 execute task，Worker 也会在执行入口阻断写操作。

## 6. 查看 Case

```text
GET /v1/cases
GET /v1/cases/{case_id}
```

重点看：

- `status`
- `evidence.observations`
- `tool_trace`
- `evidence.case_context`
- `plan.actions`
- `approvals`
- `invocations`
- `events`

## 7. 查看评估

```text
GET /v1/evals/summary
```

关注指标：

- case_resolution_rate
- verification_pass_rate
- replanned_cases
- policy_denials
- evidence_grounding_failures
- manual_handoff_cases
- task_failures

## 8. 常见问题

### PowerShell profile.ps1 报错

如果 PowerShell 启动时出现：

```text
无法加载 profile.ps1，因为在此系统上禁止运行脚本
```

这不影响 Git、Docker 或项目运行，可以暂时忽略。

### Case 进入 manual_review

先看 `events`：

- `tool_observation` 失败：通常是 ERPNext / API / 权限问题
- `evidence_grounding_failed`：模型计划缺少证据支撑
- `policy_denied`：权限策略拒绝
- `verification_failed`：写入后回读验证失败
- `manual_review_required`：Worker 可能在写操作中断，禁止盲目重试

### 真实回归记录

2026-07-19 本地触发回归：

```text
Case: 4ac6e175-88cc-4e97-82db-f602da4fec66
Order: SAL-ORD-2026-00002
Result: manual_review
Reason: ERPNext ConnectError; get_order 无法读取订单
Conclusion: Agent 安全停止，没有生成写计划，没有 ERP 写入
```

这是有效的安全行为：外部系统不可达时，Agent 不应该编造事实或继续执行。

2026-07-19 服务恢复后真实闭环：

```text
Stock setup: MAT-RECO-2026-00004
Case: 4fb78244-ad8b-4690-bd7f-226f9c782833
Order: SAL-ORD-2026-00002
Action: transfer_stock
Approval: warehouse_manager + sales_manager
ERP draft: MAT-STE-2026-00007
Result: resolved
Memory: lessons_recorded, 3 lessons
```

随后触发记忆检查：

```text
Case: fe692874-cada-4b87-a0a7-ac2f0ecb6dda
Result: waiting_approval
Memory: CaseContextBuilder returned 1 same-tenant Verified Case Lesson from the previous resolved Case
```
