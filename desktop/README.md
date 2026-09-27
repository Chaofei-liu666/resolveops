# 桌面端

`desktop/` 包含 ResolveOps 的 Windows 桌面端构建代码。最终应用由 Electron 窗口、内置 API、Worker 和 SQLite Runtime 组成；每个 Windows 用户拥有独立的配置和 Case 数据。

## 目录

| 目录 | 作用 |
|---|---|
| `electron/` | 应用窗口、系统托盘、安装包配置与发布脚本 |
| `runtime/` | PyInstaller 构建的 API / Worker Runtime |

## 使用者数据

安装后，运行时数据位于 Windows 用户目录的 `resolveops-desktop` 应用数据文件夹，包括：

- `data/resolveops.db`：Case、审批、事件和审计记录；
- `.env`：本地运行时初始配置；
- `connections.json`：从“系统配置”保存的 LLM 与 ERPNext 连接。

密钥只写入本机配置文件，工作台只显示其已配置状态。

## 构建

进入 [electron/README.md](electron/README.md) 查看 Node.js、Python、打包和 GitHub Release 发布步骤。
