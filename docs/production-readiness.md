# ResolveOps 生产就绪度评估

本文档用于判断 ResolveOps 当前是否可以上线，以及距离真实生产环境还差哪些能力。

结论先行：

```text
本地开发 / 求职展示：可以
ERPNext 沙箱试运行：可以
企业内网 staging：有条件可以
真实生产写操作上线：暂不建议
```

这里的“真实生产上线”不是指服务能启动，而是指 Agent 可以接入真实企业系统、处理真实业务数据，并在受控边界内执行写操作。

## 1. 已经具备的能力

### 1.1 真实业务系统接入

当前系统已经不是纯 mock demo。

已具备：

- 接入 ERPNext API
- 能读取真实 Sales Order、Customer、Item、Warehouse、Stock 等数据
- 能创建 ERPNext 草稿类业务单据
- 能重新查询 ERPNext 验证执行结果

这说明 Agent 已经能通过工具连接外部业务系统，而不是只生成文本。

### 1.2 Agent 主链路

当前主链路已经形成闭环：

```text
异常事件
→ 创建 Case
→ Planner 调查和生成计划
→ Tool Registry 选择工具
→ Executor 执行动作
→ Policy Engine 做权限和风险检查
→ Approval 控制人工审批
→ Verifier 回读业务系统验证结果
→ Case resolved / failed / waiting
```

这条链路可以证明 ResolveOps 是一个面向业务 Case 的 Agent，而不是单轮问答助手。

### 1.3 工具抽象

当前工具不再只是散落在代码里的 ERP 函数，而是通过工具规格和注册表组织。

已具备：

- ToolSpec
- ToolRegistry
- 只读工具和写工具边界
- tool name / description / schema / risk level / side effect 元信息
- 工具轨迹解释

这个设计的价值是：

> ERPNext 只是当前 Adapter，Agent 认知的是工具能力，而不是直接绑定 ERP 页面或某个固定流程。

### 1.4 权限与审批

当前已经具备企业 Agent 最基本的写操作安全边界。

已具备：

- operator 身份
- role-based 权限
- 写工具统一经过 Policy Engine
- approval 绑定具体 action
- approval 过期
- approval revoke
- approval 一次性消费
- 未审批不执行中高风险写操作

这比简单的 `requires_approval=True` 更接近生产系统，因为它能防止审批复用、审批过期后继续执行、计划变更后绕过审批。

### 1.5 状态持久化与上下文隔离

当前不是依赖聊天历史恢复状态。

已具备：

- Case 状态持久化
- task / event / approval / tool invocation / verification 记录
- case_id 作为业务生命周期主键
- run / tool invocation 作为执行粒度
- 多 Case 并发时按 case_id 隔离状态

这部分可以回答：

> 长任务执行到一半失败后，系统靠数据库里的结构化状态恢复，而不是靠模型回忆上下文。

### 1.6 故障注入和回归测试

当前已经有可靠性验证意识。

已具备：

- webhook 重复投递幂等
- approval 过期和撤销测试
- 工具失败处理测试
- 执行结果验证测试
- 容器化测试 profile
- 本地一键回归测试脚本

这说明项目不是只“跑通 happy path”。

### 1.7 运行健康检查

当前已经具备基础上线探针。

已具备：

- `/healthz`
- `/readyz`
- `/v1/runtime/status`
- DB 检查
- migration 检查
- queue 检查
- config readiness 检查
- local / staging / production 配置分层

这部分用于部署系统判断服务是否真的可以接流量，而不只是进程还活着。

## 2. 仍然不足的地方

### 2.1 生产身份体系还不完整

当前 operator 身份适合本地和沙箱，但还不是完整企业 IAM。

生产前应补齐：

- 接入企业 SSO / OAuth / OIDC
- operator 和企业员工账号绑定
- API Key 生命周期管理
- 密钥轮换
- 最小权限角色
- 操作人审计

当前不能把 `OPERATOR_SEED_KEYS` 带进 staging / production。

### 2.2 Secret 管理还不完整

当前支持环境变量和配置检查，但还没有真正的 Secret Manager。

生产前应补齐：

- 云厂商 Secret Manager / Vault
- 密钥轮换机制
- Secret 注入流水线
- 禁止 `.env` 进入生产镜像或服务器
- 日志脱敏检查

### 2.3 监控告警还不完整

当前有 runtime status 和事件记录，但还不是完整观测体系。

生产前应补齐：

- structured logging
- trace_id / case_id / tool_call_id 贯穿日志
- Prometheus metrics
- 告警规则
- worker 队列积压告警
- approval 长时间未处理告警
- tool failure rate 告警
- LLM 调用失败率和成本告警

### 2.4 数据备份和恢复还不完整

生产前必须补齐：

- PostgreSQL 定时备份
- 备份恢复演练
- migration 前备份
- 回滚脚本
- 审批和执行事件不可变审计策略

否则 Agent 状态一旦丢失，长任务恢复就不成立。

### 2.5 压测和容量评估还不完整

当前适合个人项目和沙箱验证，但还没有生产容量数据。

生产前应验证：

- 同时处理多少 Case
- P95 / P99 延迟
- ERP API 限流策略
- LLM API 限流策略
- worker 队列积压恢复能力
- 多 Case 同时写同一业务对象时的冲突率

不要在没有压测数据时说“支持高并发”。

更准确的表述是：

> 支持多 Case 并发执行，并通过幂等、状态持久化、审批消费和工具执行边界降低并发冲突风险。

### 2.6 写操作范围仍应保守

当前写操作应继续限制在草稿类、可验证、可撤销或低风险动作。

生产前不建议开放：

- 删除业务单据
- 自动提交正式财务影响单据
- 修改价格主数据
- 修改客户信用额度
- 自动发送不可撤回客户承诺
- 自动确认付款、发票或出库

这不是能力不足，而是企业 Agent 的正确边界。

## 3. 当前上线等级

| 等级 | 是否达标 | 说明 |
|---|---:|---|
| 本地开发 | 是 | Docker Compose 可启动，测试可跑，连接本地 / 沙箱 ERPNext |
| 求职展示 | 是 | 能展示真实工具调用、审批、验证、故障注入和 Agent 架构思考 |
| ERPNext 沙箱试运行 | 是 | 可以用测试数据跑完整闭环 |
| 企业内网 staging | 有条件 | 需要关闭 seed key，配置真实密钥，接入 staging ERP |
| 真实生产只读上线 | 有条件 | 需要 IAM、Secret、日志脱敏、监控告警 |
| 真实生产写操作上线 | 暂不建议 | 需要完整审计、备份恢复、压测、权限接入、运维流程 |

## 4. 下一阶段优先级

如果继续向上线靠近，优先级如下。

### P0：上线前必须补齐

- 企业身份认证
- Secret Manager
- 生产日志脱敏
- 数据库备份恢复
- 监控告警
- staging 环境验证

### P1：增强可靠性

- ERP / LLM 限流
- worker 队列指标
- tool failure 熔断
- 更完整的故障注入
- 多 Case 并发压测

### P2：增强 Agent 能力

- 更细的 Planner 动态调整
- 更通用的跨系统 Tool Adapter
- 轻量长期经验沉淀
- 复杂 Case 的候选方案评分

注意：P2 不是上线前最急的，因为当前项目要先证明“Agent 安全可靠地完成业务闭环”，而不是堆更多智能能力。

## 5. 面试时的推荐表述

可以这样说：

> ResolveOps 当前已经达到 ERPNext 沙箱可运行和求职展示级别，具备真实业务系统接入、工具调用、Case 状态持久化、调用级审批、结果验证、故障注入和容器化回归测试。它还没有直接进入真实生产写操作，因为生产环境还需要补齐企业 IAM、Secret 管理、监控告警、备份恢复和压测数据。我没有把“能跑 Docker”包装成“已生产上线”，而是按环境分层明确了上线边界。

这比直接说“项目已上线”更可信。

如果需要对外展示，可以先做 hosted sandbox demo，而不是生产 SaaS。云端 Demo 的范围和安全边界见 [Cloud Demo Plan](cloud-demo-plan.md)。
