const { contextBridge, ipcRenderer } = require("electron");

// The existing frontend only needs this narrow compatibility bridge to clear
// the shell-owned session on logout. No filesystem or generic IPC is exposed.
contextBridge.exposeInMainWorld("__TAURI__", {
  core: {
    invoke(command) {
      return ipcRenderer.invoke("hugagent:invoke", command);
    },
  },
});
