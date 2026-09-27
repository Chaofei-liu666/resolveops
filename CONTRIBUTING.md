# 贡献指南

感谢你参与 ResolveOps。

## 本地开发

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python scripts/dev.py
```

`scripts/dev.py` 会在同一终端启动本地 API 与 Worker，默认使用 `data/resolveops.db`。按 `Ctrl+C` 会同时停止两个进程。

另开一个终端运行测试：

```powershell
.\.venv\Scripts\Activate.ps1
.\scripts\test.ps1
```

## 修改边界

- CLI 通过 ResolveOps API 访问业务能力。
- 调查阶段向 LLM 暴露只读工具；写操作由 Action Plan 表达。
- 每个 Action Plan 依次经过 Policy、Approval、Executor 与 Verifier。
- ERPNext 位于适配器层；新增业务系统沿用相同边界。
- ERP 写入收敛在 Executor 与受控故障注入入口。
- Git 只提交源码和示例配置；密钥、本地数据库和客户数据保留在本机。

## 提交前检查

```powershell
.\scripts\test.ps1
```

提交说明应包含受影响的 Agent / Runtime 边界；用户可见行为同步更新文档。
