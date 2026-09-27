# 服务端部署（草案）

本页面向共享 Case 数据、多个 Worker 或受控网络环境。桌面端和本地开发继续使用 SQLite。

## 当前支持

- PostgreSQL `DATABASE_URL`；
- API 与 Worker 分进程启动；
- PostgreSQL migration 与启动锁；
- `/healthz`、`/readyz`、`/v1/runtime/status`；
- ERPNext 与 LLM 的环境变量配置。

## 运行前准备

准备 PostgreSQL 数据库和最小权限账号，并在密钥管理系统配置：

```text
APP_ENV=staging
DATABASE_URL=postgresql+psycopg://resolveops:<password>@<host>:5432/resolveops
ERPNEXT_BASE_URL=https://erp.example.com
ERPNEXT_API_KEY=<key>
ERPNEXT_API_SECRET=<secret>
LLM_BASE_URL=<provider>
LLM_API_KEY=<key>
LLM_MODEL=<model>
WEBHOOK_SECRET=<secret>
ENABLE_FAULT_INJECTION=false
```

API 和 Worker 使用相同的 `DATABASE_URL`。启动命令：

```powershell
python -m uvicorn production.main:app --host 0.0.0.0 --port 8090
python -m production.worker
```

首次启动后检查 `/readyz`，再通过一个 ERPNext 沙箱 Case 验证审批和写后回读。

## 上线前缺口

当前仓库适合演示和沙箱验证。正式生产接入前需要补齐：IAM/SSO、集中密钥管理、备份恢复演练、监控告警、压测、变更审批与值班流程。
