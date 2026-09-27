# 从源码启动

本文目标：启动工作台，确认 API 和 Worker 都可用，然后创建一个 Case。

## 1. 准备环境

需要 Python 3.12+ 和 Git。复制配置并安装依赖：

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

`.env` 默认将数据写入 `data/resolveops.db`。文件由 `.gitignore` 排除。

## 2. 启动

```powershell
python scripts/dev.py
```

运行时输出工作台和 API 地址。打开 <http://127.0.0.1:8090/>，并执行：

```powershell
Invoke-RestMethod http://127.0.0.1:8090/healthz
```

预期响应：

```json
{"status":"ok"}
```

`scripts/dev.py` 同时启动 API 和 Worker；按 `Ctrl+C` 后两者退出。

## 3. 连接 ERPNext 与 LLM

完整案例需要 ERPNext 沙箱。填写工作台“系统配置”，或更新 `.env`：

```text
ERPNEXT_BASE_URL=http://127.0.0.1:8080
ERPNEXT_API_KEY=<api-key>
ERPNEXT_API_SECRET=<api-secret>
LLM_BASE_URL=<provider-base-url>
LLM_API_KEY=<api-key>
LLM_MODEL=<model-name>
```

保存后，LLM 与 ERPNext 卡片会显示连接状态。ERPNext 集成账号至少需要读取订单、库存、仓库、采购和客户资料的权限。

## 4. 创建 Case

在“Case 工作台”新建 `inventory_shortage`，输入 ERPNext 中存在的订单号。也可使用 CLI：

```powershell
python resolveops.py init
python resolveops.py config set api_url http://127.0.0.1:8090
python resolveops.py config set operator_key local-ops-key
python resolveops.py case create --type inventory_shortage --order SAL-ORD-2026-00002 --reason "local demo"
```

预期状态依次为 `queued`、`running`，随后进入 `waiting_approval`、`resolved` 或 `manual_review`。在“执行轨迹”查看每一个事件。

## 5. 验证修改

```powershell
.\scripts\test.ps1
```

测试使用独立 SQLite 数据库。修改 Agent 或工具后，重启 `python scripts/dev.py` 再创建一个新 Case 验证行为。
