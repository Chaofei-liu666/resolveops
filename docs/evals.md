# 评测与故障注入

## 回归测试

```powershell
.\scripts\test.ps1
```

单元测试覆盖工具参数、上下文隔离、Action Plan、审批、幂等执行和失败恢复。测试运行在 SQLite 与本地替身上。

## 运行时指标

Case 详情和执行轨迹记录：

| 指标 | 用途 |
|---|---|
| LLM 次数、Token、延迟 | 判断模型调用开销。 |
| 工具次数、成功率、延迟 | 定位 ERP 或工具适配器瓶颈。 |
| 队列等待、总耗时 | 判断 Worker 是否饱和。 |
| Replan 次数 | 观察业务状态变化后的恢复路径。 |
| Grounding、Policy、Verification 事件 | 确认方案、审批和写后校验是否完整。 |

## 固定评测集

```powershell
python resolveops.py eval seed --suite core-v1 --order SAL-ORD-2026-00002
python resolveops.py eval summary --suite core-v1 --limit 50
python resolveops.py eval case <case-id> --events
```

`core-v1` 覆盖库存变化、审批过期、审批撤销、价格异常、交付延期、工具失败、证据不足和上下文污染。报告指标时使用固定 suite，与历史调试数据分开。

## 故障注入

故障注入作用于 ERPNext 沙箱，用于复现库存变化和外部依赖失败。仅在 `local`、`test` 或 `staging` 开启；生产配置固定为 `ENABLE_FAULT_INJECTION=false`。
