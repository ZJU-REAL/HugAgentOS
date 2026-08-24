import {
  app,
  BrowserWindow,
  dialog,
  globalShortcut,
  ipcMain,
  Menu,
  nativeImage,
  Notification,
  session,
  shell,
  Tray,
} from "electron";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { loadToken, redeem, saveToken, validate } from "./auth.mjs";
import {
  cloudBase,
  isProvisioned,
  loadConfig,
  LOCAL_SERVER_BASE,
  provision,
  provisionMode,
  saveServerBase,
  updateBase,
} from "./config.mjs";
import { loadOrCreateBridgeSecret, onCloudLogin } from "./hybrid.mjs";
import { createHttpClient } from "./http-client.mjs";
import { configureUosRuntime, startWhenReady } from "./lifecycle.mjs";
import { LocalServerManager, probe } from "./local-server.mjs";
import { startNotificationPoll } from "./notifications.mjs";
import { startProxy } from "./proxy.mjs";
import { readJson, writeJson } from "./storage.mjs";
import { checkForUpdates } from "./updater.mjs";

const sourceDir = dirname(fileURLToPath(import.meta.url));
configureUosRuntime(app);
const configDir = join(app.getPath("appData"), "com.hugagent.desktop");
app.setPath("userData", configDir);
app.setName("HugAgentOS UOS");

const hasLock = app.requestSingleInstanceLock();
if (!hasLock) app.quit();

let mainWindow = null;
let quickWindow = null;
let tray = null;
let proxy = null;
let localServer = null;
let stopNotifications = null;
let quitting = false;
let cleanupStarted = false;
let cleanupComplete = false;
let pendingDeepLinks = process.argv.filter((arg) => arg.startsWith("hugagent://"));
const runtime = {
  config: null,
  token: null,
  bridgeUser: null,
  bridgeSecret: "",
  hybridLocal: false,
  capabilityTimer: null,
  brand: { name: "HugAgentOS", website_url: "", local_service_name: "hugagent" },
};

function resourcePath(name) {
  if (app.isPackaged) return join(process.resourcesPath, name);
  if (name === "web") return join(sourceDir, "..", "..", "src", "frontend", "dist");
  return join(sourceDir, "..", "generated", name);
}

function localUrl(path = "/") {
  return `${proxy.origin}${path}`;
}

function showMain() {
  if (!mainWindow) return;
  mainWindow.show();
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
}

async function loadMain(path) {
  await mainWindow?.loadURL(path.startsWith("http") ? path : localUrl(path));
  showMain();
}

function safeExternal(raw) {
  try {
    const url = new URL(raw);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.toString() : null;
  } catch {
    return null;
  }
}

async function restartApp() {
  app.relaunch();
  app.quit();
}

async function dispatchFolderEvent(eventName) {
  const result = await dialog.showOpenDialog(mainWindow, { properties: ["openDirectory", "createDirectory"] });
  if (result.canceled || !result.filePaths[0]) return;
  await mainWindow.webContents.executeJavaScript(
    `window.dispatchEvent(new CustomEvent(${JSON.stringify(eventName)},{detail:${JSON.stringify(result.filePaths[0])}}))`,
  );
}

async function handleAction(url) {
  const name = url.searchParams.get("name") || "";
  if (name === "open-login") {
    await shell.openExternal(`${runtime.config.server_base}/?desktop=1`);
    await loadMain("/__desktop/login?waiting=1");
  } else if (name === "provision") {
    await provision(configDir, url.searchParams.get("mode"), url.searchParams.get("base") || "");
    await restartApp();
  } else if (name === "save-server") {
    await saveServerBase(configDir, url.searchParams.get("base") || "");
    await restartApp();
  } else if (name === "server-config") {
    await loadMain("/__desktop/server-config");
  } else if (name === "reload") {
    mainWindow.reload();
  } else if (name === "pick-local-folder") {
    await dispatchFolderEvent("hugagent:local-folder");
  } else if (name === "pick-grant-folder") {
    await dispatchFolderEvent("hugagent:grant-folder");
  } else if (name === "open-path") {
    const path = url.searchParams.get("path");
    if (path) await shell.openPath(path);
  } else if (name === "activate-local" && provisionMode(runtime.config) !== "dual") {
    await provision(configDir, "local_only");
    await restartApp();
  } else if (name === "activate-cloud") {
    await saveServerBase(configDir, cloudBase(runtime.config));
    await restartApp();
  }
}

function installNavigationGuards(window) {
  window.webContents.setWindowOpenHandler(({ url }) => {
    const external = safeExternal(url);
    if (external) void shell.openExternal(external);
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, raw) => {
    const url = new URL(raw);
    if (url.origin === proxy.origin) {
      if (url.pathname === "/__desktop/action") {
        event.preventDefault();
        void handleAction(url).catch((error) => dialog.showErrorBox("HugAgentOS", error.message));
        return;
      }
      const nativeActions = {
        "/__desktop/pick-local-folder": "pick-local-folder",
        "/__desktop/pick-grant-folder": "pick-grant-folder",
        "/__desktop/open-path": "open-path",
      };
      if (nativeActions[url.pathname]) {
        event.preventDefault();
        url.searchParams.set("name", nativeActions[url.pathname]);
        void handleAction(url).catch((error) => dialog.showErrorBox("HugAgentOS", error.message));
        return;
      }
      const loginLanding = url.pathname === "/login" || url.pathname.startsWith("/login/") || url.pathname.startsWith("/mock-sso") || url.pathname.includes("/sso/");
      if (!loginLanding) return;
    }
    event.preventDefault();
    runtime.token = null;
    void saveToken(configDir, null);
    void loadMain("/__desktop/login");
  });
}

async function closeMain(event) {
  if (quitting) return;
  event.preventDefault();
  const prefsPath = join(configDir, "prefs.json");
  const prefs = await readJson(prefsPath, {});
  if (prefs.close_action === "minimize") return mainWindow.hide();
  if (prefs.close_action === "exit") return app.quit();
  const result = await dialog.showMessageBox(mainWindow, {
    type: "question",
    title: `关闭 ${runtime.brand.name}`,
    message: "关闭窗口后要继续在后台运行吗？",
    buttons: ["最小化到托盘", "退出程序", "取消"],
    defaultId: 0,
    cancelId: 2,
    checkboxLabel: "记住我的选择",
  });
  if (result.response === 2) return;
  if (result.checkboxChecked) await writeJson(prefsPath, { close_action: result.response === 0 ? "minimize" : "exit" });
  if (result.response === 0) mainWindow.hide();
  else app.quit();
}

function createMainWindow(startUrl) {
  mainWindow = new BrowserWindow({
    title: runtime.brand.name,
    width: 1280,
    height: 860,
    minWidth: 960,
    minHeight: 640,
    show: false,
    backgroundColor: "#f5f6f8",
    autoHideMenuBar: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      devTools: !app.isPackaged,
      preload: join(sourceDir, "preload.cjs"),
    },
  });
  installNavigationGuards(mainWindow);
  mainWindow.on("close", (event) => { void closeMain(event); });
  mainWindow.once("ready-to-show", () => mainWindow.show());
  void mainWindow.loadURL(startUrl);
}

function toggleQuickAsk() {
  if (!runtime.token) return showMain();
  if (quickWindow && !quickWindow.isDestroyed()) {
    if (quickWindow.isVisible() && quickWindow.isFocused()) quickWindow.hide();
    else { quickWindow.show(); quickWindow.focus(); }
    return;
  }
  quickWindow = new BrowserWindow({
    title: `${runtime.brand.name} · 快速问答`,
    width: 560,
    height: 680,
    minWidth: 440,
    minHeight: 480,
    alwaysOnTop: true,
    skipTaskbar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true, preload: join(sourceDir, "preload.cjs") },
  });
  installNavigationGuards(quickWindow);
  void quickWindow.loadURL(localUrl("/?quickask=1"));
  quickWindow.on("closed", () => { quickWindow = null; });
}

function buildMenu() {
  return Menu.buildFromTemplate([
    { label: "文件", submenu: [
      { label: "新建对话", accelerator: "CmdOrCtrl+N", click: () => void loadMain("/") },
      { label: "运行模式…", click: () => void loadMain("/__desktop/init?manage=1") },
      { label: "设置服务器地址…", click: () => void loadMain("/__desktop/server-config") },
      { label: "本机服务…", click: () => void loadMain("/__desktop/setup?manage=1") },
      { type: "separator" },
      { label: "关闭时重新询问", click: () => void writeJson(join(configDir, "prefs.json"), { close_action: null }) },
      { role: "quit", label: "退出" },
    ] },
    { label: "编辑", submenu: [
      { role: "undo", label: "撤销" }, { role: "redo", label: "重做" }, { type: "separator" },
      { role: "cut", label: "剪切" }, { role: "copy", label: "复制" }, { role: "paste", label: "粘贴" }, { role: "selectAll", label: "全选" },
    ] },
    { label: "视图", submenu: [
      { role: "reload", label: "重新加载" }, { role: "togglefullscreen", label: "切换全屏" },
    ] },
    { label: "帮助", submenu: [
      { label: "检查更新…", click: () => void checkForUpdates({ app, dialog, shell, http: runtime.http, updateBase: updateBase(runtime.config) }) },
      { label: "访问官网", click: () => void shell.openExternal(runtime.brand.website_url || runtime.config.server_base) },
      { type: "separator" },
      { label: "关于", click: () => void dialog.showMessageBox({ title: "关于", message: `${runtime.brand.name} UOS 客户端`, detail: `Electron ${process.versions.electron}\n版本 ${app.getVersion()}\n目标 UOS 1070 aarch64` }) },
    ] },
  ]);
}

function buildTray() {
  let icon = nativeImage.createFromPath(resourcePath("icon.png"));
  if (icon.isEmpty()) icon = nativeImage.createFromPath(join(sourceDir, "..", "..", "desktop", "src-tauri", "icons", "32x32.png"));
  tray = new Tray(icon.resize({ width: 20, height: 20 }));
  tray.setToolTip(runtime.brand.name);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: `打开 ${runtime.brand.name}`, click: showMain },
    { label: "新建对话", click: () => void loadMain("/") },
    { label: "快速问答", click: toggleQuickAsk },
    { label: "检查更新…", click: () => void checkForUpdates({ app, dialog, shell, http: runtime.http, updateBase: updateBase(runtime.config) }) },
    { type: "separator" },
    { label: "退出", click: () => app.quit() },
  ]));
  tray.on("click", showMain);
}

async function handleDeepLink(raw) {
  try {
    const url = new URL(raw);
    const ticket = url.searchParams.get("ticket");
    if (!ticket) return;
    runtime.token = await redeem(runtime.http, runtime.config.server_base, ticket);
    await saveToken(configDir, runtime.token);
    if (runtime.hybridLocal) void onCloudLogin({
      http: runtime.http,
      cloudBase: runtime.config.server_base,
      cookieName: runtime.config.cookie_name,
      token: runtime.token,
      bridgeSecret: runtime.bridgeSecret,
      localServer,
      state: runtime,
    });
    await loadMain("/");
  } catch (error) {
    await dialog.showMessageBox({ type: "error", title: "登录失败", message: error.message });
  }
}

app.on("second-instance", (_event, argv) => {
  for (const arg of argv) if (arg.startsWith("hugagent://")) void handleDeepLink(arg);
  showMain();
});
app.on("open-url", (event, url) => { event.preventDefault(); void handleDeepLink(url); });
app.on("will-quit", () => globalShortcut.unregisterAll());
app.on("window-all-closed", () => {});

async function initializeApp() {
  app.setAsDefaultProtocolClient("hugagent");
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  runtime.brand = {
    ...runtime.brand,
    ...(await readJson(resourcePath("brand.json"), {})),
  };
  runtime.config = await loadConfig(configDir);
  runtime.http = createHttpClient({ insecureTls: runtime.config.insecure_tls });
  runtime.bridgeSecret = await loadOrCreateBridgeSecret(configDir);
  runtime.hybridLocal = provisionMode(runtime.config) === "dual";
  ipcMain.handle("hugagent:invoke", async (_event, command) => {
    if (command !== "logout_desktop") throw new Error(`不支持的桌面命令：${command}`);
    runtime.token = null;
    await saveToken(configDir, null);
    await loadMain("/__desktop/login");
  });
  const resources = {
    serverArchive: resourcePath("server-ce.zip"),
    serverManifest: resourcePath("server-ce-manifest.json"),
    runtimeArchive: resourcePath("runtime-core.tar.gz"),
    runtimeManifest: resourcePath("runtime-manifest.json"),
  };
  localServer = await new LocalServerManager({
    root: join(configDir, "local-server"),
    dataRoot: join(homedir(), ".hugagent"),
    resources,
    http: runtime.http,
    bridgeSecret: runtime.hybridLocal ? runtime.bridgeSecret : "",
    serviceName: runtime.brand.local_service_name,
  }).initialize();
  const localWanted = runtime.config.deployment_mode === "local" || runtime.hybridLocal;
  if (localWanted) localServer.prepare();
  let backendReady = runtime.config.deployment_mode === "local"
    ? await localServer.isReady()
    : await probe(runtime.http, runtime.config.server_base);
  runtime.token = await loadToken(configDir);
  if (backendReady && runtime.token && !await validate(runtime.http, runtime.config.server_base, runtime.config.cookie_name, runtime.token)) {
    runtime.token = null;
    await saveToken(configDir, null);
  }
  if (runtime.hybridLocal && runtime.token) void onCloudLogin({
    http: runtime.http,
    cloudBase: runtime.config.server_base,
    cookieName: runtime.config.cookie_name,
    token: runtime.token,
    bridgeSecret: runtime.bridgeSecret,
    localServer,
    state: runtime,
  });
  const configured = await isProvisioned(configDir);
  proxy = await startProxy({
    webDir: resourcePath("web"),
    localServer,
    getState: async () => ({
      http: runtime.http,
      serverBase: runtime.config.server_base,
      cloudServerBase: cloudBase(runtime.config),
      localBase: LOCAL_SERVER_BASE,
      token: runtime.token,
      cookieName: runtime.config.cookie_name,
      hybridLocal: runtime.hybridLocal,
      bridgeSecret: runtime.bridgeSecret,
      bridgeUser: runtime.bridgeUser,
      initMode: !configured && localServer.status.supported ? "local_only" : provisionMode(runtime.config),
      localSupported: localServer.status.supported,
      activeLocal: runtime.config.deployment_mode === "local",
      provisionMode: provisionMode(runtime.config),
      continueUrl: async () => {
        if (await localServer.isReady() && runtime.config.deployment_mode !== "local" && !runtime.hybridLocal) {
          return "/__desktop/action?name=activate-local";
        }
        return runtime.token ? "/" : "/__desktop/login";
      },
    }),
  });
  const localReady = localWanted ? await localServer.isReady() : true;
  backendReady = runtime.config.deployment_mode === "local" ? localReady : backendReady;
  const startPath = !configured
    ? "/__desktop/init"
    : (!backendReady || (runtime.hybridLocal && !localReady))
      ? "/__desktop/setup"
      : runtime.token ? "/" : "/__desktop/login";
  createMainWindow(localUrl(startPath));
  Menu.setApplicationMenu(buildMenu());
  buildTray();
  if (!globalShortcut.register("CommandOrControl+Shift+Space", toggleQuickAsk)) console.warn("[shortcut] 全局快捷键注册失败");
  stopNotifications = startNotificationPoll({ Notification, http: runtime.http, proxyOrigin: proxy.origin, state: runtime, brand: runtime.brand.name });
  for (const link of pendingDeepLinks) void handleDeepLink(link);
  pendingDeepLinks = [];
}

app.on("before-quit", (event) => {
  if (cleanupComplete) return;
  event.preventDefault();
  quitting = true;
  if (cleanupStarted) return;
  cleanupStarted = true;
  void (async () => {
    stopNotifications?.();
    if (runtime.capabilityTimer) clearInterval(runtime.capabilityTimer);
    await localServer?.shutdown();
    await proxy?.close();
    runtime.http?.destroy();
    cleanupComplete = true;
    app.quit();
  })();
});

if (hasLock) {
  startWhenReady({
    app,
    initialize: initializeApp,
    onError: (error) => {
      console.error("[startup] Electron initialization failed", error);
      dialog.showErrorBox("HugAgentOS 启动失败", error instanceof Error ? error.message : String(error));
      app.quit();
    },
  });
}
