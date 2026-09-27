const { app, BrowserWindow, ipcMain, Menu, Tray, nativeImage } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');

let mainWindow;
let serviceUrl;
let apiProcess;
let workerProcess;
let tray;
let isQuitting = false;
const logs = [];

function writeLog(message) {
  const line = `[${new Date().toLocaleTimeString('zh-CN', { hour12: false })}] ${message}`;
  logs.push(line);
  mainWindow?.webContents.send('desktop:log', line);
}

function packagedRuntimeRoot() {
  return app.isPackaged
    ? path.join(process.resourcesPath, 'runtime')
    : path.join(__dirname, '..', 'runtime');
}

function runtimeExecutable() {
  return path.join(packagedRuntimeRoot(), 'win', 'resolveops-runtime', 'resolveops-runtime.exe');
}

function parseDotEnv(contents) {
  const values = {};
  for (const rawLine of contents.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const match = line.match(/^([A-Z0-9_]+)=(.*)$/);
    if (!match) continue;
    let value = match[2].trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    values[match[1]] = value;
  }
  return values;
}

async function desktopDemoRoleKey(role) {
  const allowed = new Set(['ops_admin', 'warehouse_manager', 'sales_manager', 'procurement_manager', 'finance_manager']);
  if (!allowed.has(role)) throw new Error('不支持的本地演示角色。');
  const runtime = path.join(app.getPath('userData'), 'runtime');
  const env = parseDotEnv(await fsp.readFile(path.join(runtime, '.env'), 'utf8'));
  if (env.APP_ENV !== 'desktop') throw new Error('一键角色切换仅在本地桌面演示模式可用；其他环境请手动输入 Operator API Key。');
  if (role === 'ops_admin' && env.OPERATOR_API_KEY) return env.OPERATOR_API_KEY;
  for (const entry of (env.OPERATOR_SEED_KEYS || '').split(';')) {
    const match = entry.match(/^([^:;]+):([^:;]+):(.+)$/);
    if (match && match[2] === role && match[3]) return match[3];
  }
  throw new Error(`本机未为 ${role} 配置演示 Key。请在桌面运行目录的 .env 中设置 OPERATOR_SEED_KEYS，或手动输入 Key。`);
}

function portInUse(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(true));
    server.once('listening', () => server.close(() => resolve(false)));
    server.listen(port, '127.0.0.1');
  });
}

async function pickPort(runtime) {
  const portPath = path.join(runtime, 'desktop-port.txt');
  const saved = Number((await fsp.readFile(portPath, 'utf8').catch(() => '')).trim());
  if (Number.isInteger(saved) && saved > 0 && !(await portInUse(saved))) return saved;
  let port = 8090;
  while (await portInUse(port)) port += 1;
  await fsp.writeFile(portPath, String(port), 'utf8');
  return port;
}

async function prepareRuntime() {
  const runtime = path.join(app.getPath('userData'), 'runtime');
  await fsp.mkdir(path.join(runtime, 'data'), { recursive: true });
  const envPath = path.join(runtime, '.env');
  if (!fs.existsSync(envPath)) {
    await fsp.copyFile(path.join(packagedRuntimeRoot(), '.env.example'), envPath);
    writeLog('首次运行：已创建独立本地 SQLite 数据库与连接配置。');
  }
  const port = await pickPort(runtime);
  serviceUrl = `http://127.0.0.1:${port}/`;
  return { runtime, port };
}

function attachOutput(child, label) {
  const append = (chunk) => String(chunk).trim().split(/\r?\n/).filter(Boolean)
    .forEach((line) => writeLog(`${label} · ${line}`));
  child.stdout?.on('data', append);
  child.stderr?.on('data', append);
  child.on('error', (error) => writeLog(`${label} 启动失败：${error.message}`));
  child.on('exit', (code) => {
    if (code !== 0 && code !== null) writeLog(`${label} 已退出（code ${code}）。`);
  });
}

function stopRuntime() {
  for (const child of [apiProcess, workerProcess]) {
    if (child && !child.killed) child.kill();
  }
  apiProcess = undefined;
  workerProcess = undefined;
}

function spawnRuntime(mode, runtime, port) {
  const executable = runtimeExecutable();
  if (!fs.existsSync(executable)) {
    throw new Error('内置 Runtime 缺失。请重新安装完整的 ResolveOps Desktop 安装包。');
  }
  const child = spawn(executable, mode === 'api' ? ['api', '--port', String(port)] : ['worker'], {
    cwd: runtime,
    windowsHide: true,
    env: { ...process.env, RESOLVEOPS_RUNTIME_CONFIG_PATH: path.join(runtime, 'connections.json') },
  });
  attachOutput(child, mode === 'api' ? 'API' : 'Worker');
  return child;
}

function apiReady() {
  return new Promise((resolve) => {
    const request = http.get(`${serviceUrl}healthz`, (response) => {
      response.resume();
      resolve(response.statusCode >= 200 && response.statusCode < 300);
    });
    request.setTimeout(3000, () => { request.destroy(); resolve(false); });
    request.once('error', () => resolve(false));
  });
}

async function waitForApi() {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    if (await apiReady()) return;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error('本地 API 未在 30 秒内就绪。请在启动日志中检查 Runtime 输出。');
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 1080,
    minHeight: 720,
    backgroundColor: '#f6f8fc',
    title: 'ResolveOps Agent · 企业 ERP 订单异常处理',
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, sandbox: true },
  });
  mainWindow.setMenuBarVisibility(false);
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'boot.html'));
  mainWindow.on('close', (event) => {
    if (isQuitting) return;
    event.preventDefault();
    mainWindow.hide();
    writeLog('窗口已隐藏到系统托盘；本地 Agent Runtime 继续运行。');
  });
}

function showMainWindow() {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function trayImage() {
  // Windows notification areas consistently render PNG, while inline SVG can
  // become transparent on some Electron/Windows combinations.
  return nativeImage.createFromDataURL('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAU0lEQVR4nO3SyREAEAxAUX3oUeGa4OrAxFgSy/8zuSHvwDkq8iEmjTFd3kQAkC70BEANMHMGAIA3AK2kNwBs+QMAzAGjGAAA7gOsnvMAmojq8m/LdfRRcaYPWV8AAAAASUVORK5CYII=');
}

function createTray() {
  tray = new Tray(trayImage());
  tray.setToolTip('ResolveOps Agent · 本地运行中');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开 ResolveOps', click: showMainWindow },
    { type: 'separator' },
    {
      label: '退出 ResolveOps',
      click: () => { isQuitting = true; app.quit(); },
    },
  ]));
  tray.on('click', showMainWindow);
}

async function startDesktop() {
  try {
    stopRuntime();
    writeLog('正在准备内置本地 Runtime…');
    const { runtime, port } = await prepareRuntime();
    writeLog(`本地 API：${serviceUrl}`);
    apiProcess = spawnRuntime('api', runtime, port);
    writeLog('正在初始化 SQLite 与 API…');
    await waitForApi();
    workerProcess = spawnRuntime('worker', runtime, port);
    writeLog('本地 Worker 已启动；无需 Docker。');
    // The local API keeps the same loopback URL between launches.  Clear the
    // Chromium cache before loading it so an installed upgrade cannot render
    // an older cached index.html/app.js bundle.
    await mainWindow.webContents.session.clearCache();
    await mainWindow.loadURL(serviceUrl);
  } catch (error) {
    writeLog(`[错误] ${error.message}`);
    mainWindow?.webContents.send('desktop:failed', error.message);
  }
}

app.whenReady().then(async () => {
  app.setAppUserModelId('com.resolveops.desktop');
  createWindow();
  createTray();
  ipcMain.handle('desktop:logs', () => logs);
  ipcMain.handle('desktop:retry', async () => startDesktop());
  ipcMain.handle('desktop:demo-role-key', async (_event, role) => desktopDemoRoleKey(String(role || '')));
  await startDesktop();
});

app.on('before-quit', () => {
  isQuitting = true;
  stopRuntime();
  tray?.destroy();
});
app.on('activate', showMainWindow);
