const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('resolveOpsDesktop', {
  logs: () => ipcRenderer.invoke('desktop:logs'),
  retry: () => ipcRenderer.invoke('desktop:retry'),
  demoRoleKey: (role) => ipcRenderer.invoke('desktop:demo-role-key', role),
  onLog: (callback) => ipcRenderer.on('desktop:log', (_event, line) => callback(line)),
  onFailed: (callback) => ipcRenderer.on('desktop:failed', (_event, message) => callback(message)),
});
