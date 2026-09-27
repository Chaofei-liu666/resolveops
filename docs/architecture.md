# 架构与扩展点

## 一个库存短缺 Case

```text
API 创建 Case 和 investigate Task
→ Worker 领取 Task
→ 读取订单、目标仓库存、可选调拨路线和在途采购
→ LLM 返回 Action Plan
→ Evidence Grounding 将参数与读取结果逐项比对
→ Policy 创建所需角色的 Approval
→ Executor 创建 ERP 草稿
→ Verification 回读业务单据
```

每一步都写入 Event。页面只展示这些持久化结果，因此刷新、重开工作台或切换身份不会丢失执行轨迹。

## 组件职责

| 组件 | 职责 |
|---|---|
| FastAPI | 接收 webhook 和工作台请求，创建 Case、查询状态、处理审批。 |
| Worker | 领取 Task，运行调查、重规划和执行。 |
| CaseContextBuilder | 为当前 Case 组装订单、历史事件、已确认事实与可用工具范围。 |
| ToolProfile / Scheduler | 选择工具、校验参数、去重并记录调用延迟。 |
| LLMGateway | 适配 Chat Completions，返回统一的消息、工具调用和用量数据。 |
| Evidence / Policy | 校验方案证据和审批要求。 |
| Executor / Verifier | 处理 ERP 写入、幂等键和写后回读。 |

## 数据与一致性

本地 Runtime 使用 SQLite；共享服务使用 PostgreSQL。Case、Task、Approval、Invocation 和 Event 位于同一数据库。

- Task 带 lease。调查任务可重新排队；可能已经写入 ERP 的任务进入人工处理。
- Approval 绑定 Case、Plan Version 和 Action Hash。计划变化后，旧审批失效。
- Invocation 使用幂等键。重复执行会返回已有外部单据，而不是重复创建。
- SQLite 使用 WAL 和 busy timeout；PostgreSQL 启动时执行版本化 migration。

## 扩展一个案例类型

1. 在 `CaseProfile` 中声明事件类型和工具集。
2. 在 `tools.py` 增加只读业务工具或适配器调用。
3. 在 `actions.py` 定义 Action Plan 输入。
4. 在 `policy.py` 定义风险和审批规则。
5. 在 `executors.py` 实现 preflight、写入和 verification。
6. 为正常、证据不足、审批拒绝和外部失败补测试。
