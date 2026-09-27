# 运行与排障

## 检查顺序

```powershell
Invoke-RestMethod http://127.0.0.1:8090/healthz
Invoke-RestMethod http://127.0.0.1:8090/readyz
python resolveops.py doctor
```

`healthz` 检查 API 进程；`readyz` 继续检查数据库和配置；`doctor` 会读取 API、运行状态和 ERPNext 沙箱状态。

## 常见现象

| 现象 | 检查项 | 处理 |
|---|---|---|
| 工作台打不开 | `scripts/dev.py` 输出、8090 端口 | 重启运行时或更换占用端口。 |
| ERPNext 认证失败 | Base URL、Key、Secret | 在系统配置运行“只读连通性检查”。 |
| Case 停在待审批 | Approval 的 `required_roles` | 切换到匹配的本地身份，再批准或驳回。 |
| Case 进入人工处理 | `handoff`、`worker_failure`、`verification_failed` 事件 | 按事件中的原因核实 ERP 事实。 |
| 配置修改无效 | Worker 是否已领取下一任务 | 重启 API 和 Worker；数据库 URL 修改后必须重启。 |

## 审批状态

| 操作 | 状态变化 |
|---|---|
| 批准 | `approved`，Worker 可执行当前 Plan。 |
| 驳回重规划 | 当前 Plan 失效，重新创建调查 Task。 |
| 撤销 | 停止执行，Case 转人工处理。 |
| 过期 | Worker 阻断执行，Case 转人工处理。 |

Worker 在可能写 ERP 时退出后，Case 会进入人工处理。根据 Invocation 的 idempotency key 在 ERP 中确认结果，再决定恢复方案。

## 本地数据

`data/resolveops.db` 保存 Case、审批、任务和审计记录。`.resolveops-runtime/connections.json` 保存本地连接配置。停止运行时后删除数据库文件可创建全新的演示环境；需要保留记录时先复制该文件。
