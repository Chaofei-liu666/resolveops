# ResolveOps 部署与上线安全清单

本文档定义 ResolveOps 从本地开发到沙箱 / 生产环境的最小上线要求。它不是云厂商部署手册，而是说明上线前必须满足哪些安全、配置、验证和回滚条件。当前生产就绪度判断见 [生产就绪度评估](production-readiness.md)。

## 1. 环境分层

ResolveOps 通过 `APP_ENV` 区分运行环境：

```text
local
staging
production
```

### local

用于本地开发和求职项目演示。

允许：

- 使用 `.env`
- 使用 `OPERATOR_SEED_KEYS` 初始化测试审批人
- LLM 暂时缺失时走 deterministic fallback
- 连接 ERPNext 沙箱或本地 ERPNext

要求：

- 不能连接真实生产 ERP 数据
- 只能使用测试 API Key
- 只允许创建草稿类业务记录

### staging

用于内网沙箱试运行。

要求：

- 禁止占位密钥，例如 `replace-me`
- 禁止 `OPERATOR_SEED_KEYS`
- 必须配置 LLM
- 必须配置真实 webhook secret
- 必须使用独立 ERPNext 测试租户
- `/readyz` 必须返回 ready
- `docker compose --profile test run --rm test` 必须通过

### production

生产环境要求比 staging 更严格。

当前项目还不建议直接进入真实生产。生产前至少需要补齐：

- SSO / IAM / API Gateway 级身份接入
- Secret Manager 或 Docker / K8s Secret
- HTTPS / 反向代理 / 访问控制
- 数据库备份与恢复演练
- Worker 监控和告警
- 真实压测数据
- 发布审批和回滚流程

## 2. 禁止上线的配置

以下配置在 `APP_ENV=staging` 或 `APP_ENV=production` 时会被 runtime readiness 判定为错误：

```text
ERPNEXT_API_KEY=replace-me
ERPNEXT_API_SECRET=replace-me
WEBHOOK_SECRET=replace-me
OPERATOR_API_KEY=replace-me
LLM_BASE_URL=https://api.example.com/v1
LLM_API_KEY=replace-me
LLM_MODEL=replace-me
OPERATOR_SEED_KEYS=...
```

检查方式：

```powershell
Invoke-RestMethod http://localhost:8090/readyz
```

管理员查看详细配置状态：

```powershell
$headers = @{ "X-Operator-Key" = "<ops-admin-key>" }
Invoke-RestMethod http://localhost:8090/v1/runtime/status -Headers $headers
```

重点看：

```text
checks.configuration.ok
checks.configuration.errors
checks.configuration.warnings
```

## 3. 最小部署拓扑

当前最小可运行拓扑：

```text
ERPNext sandbox
    ↓ webhook / REST API
ResolveOps API
    ↓ PostgreSQL
ResolveOps Worker
```

API 负责：

- 接收 webhook
- 创建 Case
- 提供审批、审计、查询接口
- 暴露 `/healthz`、`/readyz`、`/v1/runtime/status`

Worker 负责：

- 领取 Task
- 调用 LLM
- 调用只读业务工具
- 生成 Action Plan
- 经过 Policy / Approval 后执行写工具
- 做 read-after-write verification

## 4. 上线前检查步骤

### 4.1 构建并启动

```powershell
docker compose up -d --build
```

### 4.2 检查容器

```powershell
docker compose ps
```

API、Worker、PostgreSQL 都必须处于 running / healthy。

### 4.3 运行回归测试

```powershell
docker compose --profile test run --rm test
```

当前基线：

```text
64 passed, 1 skipped
```

### 4.4 检查 readiness

```powershell
Invoke-RestMethod http://localhost:8090/readyz
```

期望：

```json
{"status": "ready"}
```

### 4.5 检查 migration

```powershell
docker compose exec -T postgres psql -U resolveops -d resolveops -c "select version, filename from schema_migrations order by version;"
```

所有 `production/migrations/*.sql` 都必须出现在 `schema_migrations`。

### 4.6 检查队列

```powershell
$headers = @{ "X-Operator-Key" = "<ops-admin-key>" }
Invoke-RestMethod http://localhost:8090/v1/runtime/status -Headers $headers
```

重点：

```text
queues.queued
queues.running
queues.failed
```

上线前不应存在未知来源的大量 queued / running task。

## 5. 发布限制

当前版本只允许在沙箱环境验证：

- 调拨草稿
- 采购申请草稿
- 价格复核记录
- 供应商跟进记录

不允许 Agent 自动执行：

- 删除业务记录
- 修改正式订单价格
- 正式提交不可逆 ERP 单据
- 修改付款、开票、财务凭证
- 绕过审批直接写入 ERP

这些限制必须通过 Policy / Executor 边界控制，而不是只靠 prompt。

### 故障注入开关

`ENABLE_FAULT_INJECTION` 只能用于 local / test / staging。该能力允许用户从 CLI/API 触发 ResolveOps，再由 `ERPNextAdapter` 通过 ERPNext REST API 修改沙箱业务状态，例如提交 Stock Reconciliation 来模拟库存变化。

生产环境必须满足：

```text
ENABLE_FAULT_INJECTION=false
```

如果 `APP_ENV=production` 仍开启故障注入，runtime readiness 会标记为 degraded，故障注入 API 也会直接拒绝请求。

## 6. 回滚和停止

### 停止 Worker

如果发现 Agent 计划异常，优先停止 Worker，避免继续执行新任务：

```powershell
docker compose stop worker
```

API 可以继续提供 Case 查询和审批撤销。

### 撤销审批

如果某个审批已经批准但还未执行：

```text
POST /v1/approvals/{approval_id}/revoke
```

撤销后 Case 进入 `manual_review`，Worker 执行入口会阻断残留 execute task。

### 停止全部服务

```powershell
docker compose down
```

注意：默认不会删除 PostgreSQL volume。不要在未备份时删除 volume。

### 数据恢复

当前项目尚未实现自动备份。进入生产前必须补齐：

- PostgreSQL 定时备份
- 备份恢复演练
- migration 前备份
- schema 回滚策略

## 7. 上线状态判断

当前 ResolveOps 状态：

```text
本地演示 / 求职展示：可以
ERPNext 沙箱试运行：可以
真实生产上线：暂不建议
```

原因：

- 业务闭环和安全执行边界已经具备
- 但生产级身份体系、Secret 管理、监控告警、备份恢复和压测尚未完整

推荐表述：

> ResolveOps 已完成沙箱可运行 MVP，具备真实 ERPNext 接入、工具调用、证据驱动规划、审批治理、幂等执行、结果验证和可观测运行状态；下一阶段补齐生产 IAM、Secret 管理、备份恢复、告警和部署流水线后，才进入真实生产。
