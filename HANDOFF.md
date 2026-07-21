# ResolveOps 项目交接文档

本文用于把当前 ResolveOps 项目交接给另一个 Codex 账号继续开发和运行。

## 1. 项目目标

ResolveOps 是一个 API-first 的企业订单履约异常处理 Agent。它不是 ERP 聊天机器人，也不是单纯 workflow demo。

核心目标是：当企业业务系统发现订单履约异常后，Agent 自动调查相关业务证据，生成可执行方案，经过权限/审批控制后执行低风险或已授权动作，并通过读取真实业务系统状态完成结果验证。

当前参考业务系统是 ERPNext。ERPNext 只是沙箱系统和适配器，不是项目边界。未来可以替换为 SAP、WMS、CRM、工单系统、采购系统或企业内部平台。

## 2. 当前项目状态

当前已经完成的主线能力：

- Case 驱动的异常处理流程，而不是一次性聊天；
- 支持多种异常类型：
  - `inventory_shortage`：库存不足；
  - `price_mismatch`：价格异常；
  - `delivery_delay`：交付延期；
  - `supplier_delay`：供应商延期；
- 工具定义已从“ERP 函数调用”重构为 Tool Registry / Tool Spec / Tool Profile 的形式；
- Agent 根据 Case 类型选择需要的只读工具；
- LLM Planner 根据工具观察结果生成行动方案；
- Evidence Grounding 对 LLM 计划做确定性证据校验；
- Policy Engine 决定动作是否自动、审批、拒绝或转人工；
- Approval 绑定 Case、plan_version、action_hash，并支持过期、撤销、一次性消费；
- Executor 通过受控入口执行写操作，不让 LLM 直接写 ERP；
- 写操作后做 read-after-write verification；
- 支持业务状态变化后的 replanning/manual_review；
- PostgreSQL 保存 Case、Task、Event、Approval、Invocation、Operator、Case Lesson；
- 支持故障注入，用于验证异常恢复能力；
- 支持 CLI 交互，包括顶层 chat、Case chat、Case 创建、Case 查看、审批、故障注入等；
- Windows 启动脚本 `resolveops.cmd` 已支持自动初始化本地 CLI 配置、尝试启动 Docker 服务、打开 ResolveOps chat；
- 顶层 chat 已接入无工具 LLM 对话，支持短期上下文滑动窗口；
- 顶层 chat 可以回答普通无工具问题，不再强制每句拉回订单业务；
- 顶层 chat 可以显示服务端配置的 `LLM_MODEL` / `LLM_BASE_URL`，但不会泄露 API Key。

当前仍然不是完整生产系统，但已经具备个人求职项目里比较关键的企业 Agent 工程点：工具边界、权限、审批、状态、验证、故障恢复和可观测轨迹。

## 3. 当前架构

主流程：

```text
Business Exception
  -> Case
  -> Context Builder
  -> Tool Profile Router
  -> Read Tool Scheduler
  -> LLM Planner
  -> Evidence Grounding
  -> Policy Engine
  -> Bound Approval
  -> Governed Executor
  -> Read-after-write Verifier
  -> resolved / replan / manual_review
```

核心模块：

```text
CLI / HTTP API / ERPNext Webhook
        |
        v
FastAPI Control Plane
        |
        +--> Auth / Operator / Audit
        +--> Case API
        +--> Operator Chat API (/v1/chat)
        +--> Case Ask API (/v1/cases/{case_id}/ask)
        +--> Approval API
        +--> Fault Injection API
        |
        v
PostgreSQL
  - cases
  - tasks
  - events
  - approvals
  - invocations
  - operators
  - case_lessons
        |
        v
Worker / Agent Runtime
        |
        +--> CaseContextBuilder
        +--> ToolRegistry / ToolProfileRouter
        +--> BusinessReadTools
        +--> LLMGateway
        +--> Planner
        +--> Evidence Grounding
        +--> Policy Engine
        +--> ExecutorRegistry
        +--> Verifier
        +--> ERPNextAdapter
```

## 4. 上下文管理策略

当前上下文分三层：

### 4.1 顶层 chat 上下文

位置：`python resolveops.py chat`，后端接口 `POST /v1/chat`。

用途：普通无工具对话、解释项目、创建 Case 前的自然语言入口。

策略：

- CLI 在当前会话内保存最近 6 轮，即最多 12 条 `user/assistant` message；
- 每次调用 `/v1/chat` 时发送 `history`；
- 后端 `OperatorChatAgent` 使用滑动窗口历史理解“刚刚”“继续”“为什么”；
- 不写数据库，重启 CLI 后清空；
- 顶层 chat 不允许 ERP 工具，不允许业务写操作，不声称实时业务事实。

这是短期对话上下文，不是长期记忆。

### 4.2 Case 上下文

Case chat 不能只靠聊天历史。它主要依赖数据库中的结构化 Case 状态：

- Case 基本信息；
- Evidence；
- Plan；
- Approval；
- Event trace；
- Tool trace；
- Invocation；
- Verification result。

这样做是为了保证 Case 隔离：同时处理多个 Case 时，每个 Case 的上下文必须由 `case_id` 限定，不能把 A Case 的聊天历史带到 B Case。

后续可以补充 Case-scoped chat history，但必须按 `case_id` 存储和读取，不能使用全局历史。

### 4.3 长期记忆 / 经验沉淀

当前没有引入 Mem0、向量数据库或复杂知识图谱。原因是这个阶段更重要的是安全执行、验证和状态恢复。

已有轻量方向：Case 成功后记录可复用 lesson，作为后续规划提示。这比把所有聊天记录塞进向量库更适合当前项目。

## 5. 工具设计边界

当前工具不是“ERP 页面里的按钮”，而是 ResolveOps 暴露给 Agent 的外部世界接口。

设计原则：

- 只读工具可以被 Planner 调用，用于调查证据；
- 写操作也是工具/动作，但不直接暴露给 LLM 执行；
- LLM 只能生成 action plan；
- 写动作必须经过 Policy、Approval、Executor、Verification；
- ERPNextAdapter 只是其中一个系统适配器，未来可替换。

面试表述建议：

> 我没有让 LLM 直接拿 ERP API 写数据，而是把写操作降级为受控 action。模型负责提出计划，Policy 决定能不能做，Approval 绑定具体参数，Executor 执行，Verifier 再读源系统确认结果。

## 6. 本地运行方式

### 6.1 克隆项目

```powershell
git clone https://github.com/Chaofei-liu666/resolveops.git
cd resolveops
```

### 6.2 准备环境变量

复制模板：

```powershell
Copy-Item .env.example .env
```

至少需要配置：

```text
APP_ENV=local
DATABASE_URL=postgresql+psycopg://resolveops:resolveops@postgres:5432/resolveops
WEBHOOK_SECRET=local-webhook-secret
OPERATOR_API_KEY=local-ops-key
LLM_BASE_URL=<chat-completions-compatible-base-url>
LLM_API_KEY=<llm-api-key>
LLM_MODEL=<model>
```

如果要跑真实 ERPNext 沙箱，还要配置：

```text
ERPNEXT_BASE_URL=<erpnext-url>
ERPNEXT_API_KEY=<erpnext-api-key>
ERPNEXT_API_SECRET=<erpnext-api-secret>
ERPNEXT_COMPANY=<company>
ERPNEXT_STOCK_DIFFERENCE_ACCOUNT=<account>
```

注意：不要提交真实 `.env`。

### 6.3 启动服务

开发环境建议每次代码改动后 rebuild：

```powershell
docker compose up -d --build api worker
```

首次启动或全量启动：

```powershell
docker compose up -d --build
```

查看状态：

```powershell
python resolveops.py status
```

### 6.4 初始化 CLI 配置

```powershell
python resolveops.py init
python resolveops.py config set api_url http://localhost:8090
python resolveops.py config set operator_key local-ops-key
python resolveops.py config show
```

CLI 配置文件位置：

```text
C:\Users\<user>\.resolveops\config.json
```

它只保存 CLI 连接 ResolveOps API 的地址和 operator key。不要把这个文件提交到 GitHub。

### 6.5 使用 CLI

打开顶层 chat：

```powershell
python resolveops.py chat
```

Windows 可直接双击：

```text
resolveops.cmd
```

顶层 chat 常用命令：

```text
/status          查看运行状态
/cases           查看最近 Case
/new             交互式创建 Case
/case <case-id>  进入某个 Case 的 Agent chat
/help            帮助
/exit            退出
```

Case 相关命令：

```powershell
python resolveops.py case list
python resolveops.py case show <case-id>
python resolveops.py case chat <case-id>
```

审批示例：

```powershell
python resolveops.py --operator-key <warehouse-key> approval approve <approval-id>
python resolveops.py --operator-key <sales-key> approval approve <approval-id>
```

## 7. 测试方式

语法检查：

```powershell
python -m py_compile production/operator_chat.py production/main.py production/cli.py
```

单元测试：

```powershell
python -m pytest -q
```

Docker 测试：

```powershell
docker compose --profile test run --rm test
```

本次交接前已跑过的聚焦测试：

```text
tests/test_cli.py::test_cli_top_level_chat_answers_without_case
tests/test_cli.py::test_cli_top_level_chat_sends_recent_history
tests/test_reliability_rules.py::test_operator_chat_endpoint_uses_llm_agent_and_audits
tests/test_reliability_rules.py::test_operator_chat_answers_configured_model_from_system_config
```

结果：4 passed。

## 8. 当前已知问题 / 注意事项

1. Docker Desktop 必须先能启动。`resolveops.cmd` 会尝试启动 Docker Desktop，但如果 Docker daemon 长时间不可用，需要手动打开 Docker Desktop。
2. `resolveops.cmd` 默认是启动服务，不一定 rebuild。代码改动后请运行：

   ```powershell
   docker compose up -d --build api worker
   ```

3. 顶层 chat 有短期上下文，但 Case chat 目前主要依赖 Case 结构化状态，尚未加入独立 Case 问答历史表。
4. ERPNext 不在本仓库 Compose 内，需要单独准备 ERPNext 沙箱。
5. `.env`、本地数据库、CLI 配置、API Key 都不应提交。
6. 本地 PowerShell profile 在某些环境可能报执行策略错误，不影响项目本身；可以用普通 cmd、Windows Terminal 或禁用 profile 后运行。

## 9. 下一步开发建议

优先级从高到低：

1. Case-scoped chat history：给 `/v1/cases/{case_id}/ask` 增加最近问答历史，但必须按 `case_id` 隔离。
2. 完善 `resolveops.cmd` 的开发模式：可增加 `resolveops.cmd --build` 或单独 `dev-start.cmd`，避免旧镜像问题。
3. 增加 CLI 对自然语言创建 Case 的能力：例如用户直接说“创建一个 SAL-ORD-xxx 的库存不足异常”，CLI/后端提取类型、订单号和原因后创建 Case。
4. 补充更多非库存类 Case 的完整闭环，让项目不显得只会仓库调拨。
5. 增强 eval 报告，把 Case 成功率、验证通过率、转人工原因、工具失败率整理成更适合面试展示的输出。
6. 如果后续真要做长期记忆，优先做 Case Lesson 的查询和版本化，不建议一开始引入 Mem0/向量数据库。

## 10. 面试表述主线

推荐主线：

> ResolveOps 是一个企业订单履约异常处理 Agent。它不是把 ERP API 包成聊天机器人，而是围绕业务 Case 做证据调查、计划生成、策略审批、受控执行和结果验证。LLM 负责规划和解释，工具层负责连接业务系统，Policy/Approval/Executor/Verifier 保证写操作安全可审计。

重点不要说“用了很多框架”，而要讲清楚：

- 为什么正常流程不用 Agent，异常分支才用；
- 为什么写工具不能直接暴露给 LLM；
- 为什么审批要绑定具体 action hash；
- 为什么工具 success 后还要重新读取 ERP 验证；
- 为什么 Case 上下文必须隔离；
- 为什么当前没有硬塞 Mem0/向量库/多 Agent。
