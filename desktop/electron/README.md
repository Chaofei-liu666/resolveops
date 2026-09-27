# ResolveOps Electron Desktop

这是 Windows 桌面端源码。Electron 创建原生 `BrowserWindow`，在窗口内加载 ResolveOps Workbench，并启动内置 API、Worker 与 SQLite 数据库。

## 发布者构建安装包

在 Windows 上安装 Node.js 22 LTS（含 npm）和 Python 3.11+ 后，在项目根目录执行：

```powershell
cd desktop\electron
npm install
npm run dist:win
```

网络无法下载 Electron 构建依赖时，可在同一 `cmd` 窗口设置镜像后重试：

```cmd
set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
set ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/
npm run dist:win
```

生成的 NSIS 安装包位于 `desktop\electron\dist`。将该安装包作为 GitHub Release 附件分发；不要提交 `dist` 或 `node_modules`。使用者安装后从开始菜单或桌面快捷方式打开 `ResolveOps Desktop`。

## 使用者前置条件

安装包内含 API、Worker 与 SQLite 运行时。首次打开后，在“系统配置”填写 LLM 与可访问的 ERPNext 地址；每位使用者的数据与密钥都保存在自己的 Windows 用户目录中。

ERPNext 位于同一台 Windows 电脑时，可填写 `http://127.0.0.1:端口`；如果 ERP 部署在内网或公网，填写该服务可访问的 HTTP(S) 地址。
