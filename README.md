# ResolveOps

订单履约异常处置 Agent，聚焦库存不足这一类异常，不把正常的确定性业务流程伪装成 Agent。

`production/` 是可部署服务：ERPNext 通过 REST API 接入，PostgreSQL 保存 Case/事件/审批/工具调用，API 和 Worker 独立运行。根目录旧的本地界面仅保留作交互原型，不是上线路径。

## 已实现的闭环

`异常 Case → 只读调查 → 证据与计划 → 调用级审批 → 幂等执行 → 独立验证 → 关闭/重新规划`

- 所有执行计划都包含证据、预期结果、风险和验证方式；
- 审批绑定 Case、计划版本和行动参数哈希，执行一次后即消费；
- 写入前再次查询业务状态；
- 调拨单带幂等键，超时重试不会重复创建；
- 对同一“来源仓库 + SKU”的写操作使用持久化资源锁，避免多个 Case 同时占用同一库存；
- 写入接口返回成功不代表完成，必须再次查询并验证。
- Worker 租约过期时，只读调查可安全重排；可能已写入 ERP 的任务一律停止，转人工按幂等键核验。
- 填入 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 后，Agent 在最多 8 次只读 Tool 调用预算内动态决定调查顺序；模型无法获得 ERP 写工具。

数据由内置 `ERPAdapter` 提供，以便无外部账号也能演示完整流程。替换该适配器即可接入 ERPNext REST API；Case、审批、事件和验证结果仍保留在本地 SQLite 中。

## 部署前置条件

1. ERPNext 创建专用服务账号，只赋予 Sales Order、Bin 读取以及 Stock Entry 草稿创建权限。
2. 在 Stock Entry 增加 `custom_resolveops_idempotency_key` 自定义字段（唯一索引）；它用于写操作的不确定结果恢复。
3. 由 ERPNext 或集成层发送 `inventory_shortage` Webhook，Body 至少包含 `tenant_id`、`order_id`、`event`，并用 `WEBHOOK_SECRET` 计算 `sha256=<HMAC>` 签名。
4. 将审批 API 放在企业 SSO/API Gateway 后；当前 `X-Operator-Key` 是部署初始保护，后续应由网关注入经验证的操作人身份。

## 启动生产服务

```powershell
Copy-Item .env.example .env
# 填入实际 ERPNext 地址与密钥；不要提交 .env
docker compose up --build -d
```

API 在 `http://127.0.0.1:8080`，Worker 单独从 PostgreSQL 领取任务。`/healthz` 可供负载均衡健康检查使用。

## 本地原型（非上线路径）

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload --port 8080
```

打开 <http://127.0.0.1:8080>，选择 `CASE-1042`，依次点击“开始调查”、“批准本次行动”、“执行并验证”。

## 刻意未加入的组件

本版没有 Redis、向量数据库、多 Agent、MCP 或 LangGraph：它们不解决此 MVP 的核心风险。先验证证据驱动规划、审批边界、可追溯执行和结果验证，再根据真实接入需求演进。

这里的锁由 SQLite 唯一约束实现，适合单机演示。生产多副本部署时，应将同一接口替换为 PostgreSQL 的事务锁/咨询锁（或有明确租约与续租机制的分布式协调服务）。
