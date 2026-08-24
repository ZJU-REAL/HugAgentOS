import { EventEmitter } from "node:events";
import { closeSync, openSync } from "node:fs";
import {
  appendFile,
  copyFile,
  mkdir,
  readFile,
  readdir,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { homedir } from "node:os";
import { basename, join } from "node:path";
import { spawn } from "node:child_process";
import {
  activeRelease,
  installPayloads,
  localPayloadSupported,
  needsInstall,
  pruneOld,
  restorePrevious,
} from "./local-payload.mjs";
import { LOCAL_SERVER_BASE } from "./config.mjs";

const BACKUP_FILES = ["data.db", "data.db-wal", "data.db-shm", "milvus.db", "config.env", "secrets.json", "catalog.json"];
const MAX_BACKUPS = 3;

async function pathExists(path) {
  try { await stat(path); return true; } catch { return false; }
}

async function tail(path, limit = 80) {
  try {
    return (await readFile(path, "utf8")).split(/\r?\n/).filter(Boolean).slice(-limit);
  } catch {
    return [];
  }
}

async function backupData(dataRoot, backupsRoot) {
  const present = [];
  for (const file of BACKUP_FILES) if (await pathExists(join(dataRoot, file))) present.push(file);
  if (!present.length) return null;
  const destination = join(backupsRoot, new Date().toISOString().replaceAll(/[:.]/g, "-"));
  await mkdir(destination, { recursive: true });
  for (const file of present) await copyFile(join(dataRoot, file), join(destination, file));
  await writeFile(join(destination, "manifest.json"), `${JSON.stringify({ schema: 1, files: present }, null, 2)}\n`);
  return destination;
}

async function restoreData(dataRoot, backup) {
  const manifest = JSON.parse(await readFile(join(backup, "manifest.json"), "utf8"));
  await mkdir(dataRoot, { recursive: true });
  for (const file of BACKUP_FILES) await rm(join(dataRoot, file), { force: true });
  for (const file of manifest.files || []) await copyFile(join(backup, file), join(dataRoot, file));
}

async function pruneBackups(root) {
  const entries = (await readdir(root, { withFileTypes: true }).catch(() => []))
    .filter((entry) => entry.isDirectory())
    .sort((a, b) => b.name.localeCompare(a.name));
  for (const entry of entries.slice(MAX_BACKUPS)) await rm(join(root, entry.name), { recursive: true, force: true });
}

export async function probe(httpClient, base, expectedService = null) {
  try {
    const response = await httpClient.fetch(`${base.replace(/\/$/, "")}/health`, { timeout: 3_000 });
    if (!response.ok) return false;
    if (!expectedService) return true;
    return (await response.json())?.service === expectedService;
  } catch {
    return false;
  }
}

export class LocalServerManager extends EventEmitter {
  constructor({ root, dataRoot = join(homedir(), ".hugagent"), resources, http, bridgeSecret = "", serviceName = "hugagent" }) {
    super();
    this.root = root;
    this.dataRoot = dataRoot;
    this.resources = resources;
    this.http = http;
    this.bridgeSecret = bridgeSecret;
    this.serviceName = serviceName;
    this.child = null;
    this.installing = false;
    this.shuttingDown = false;
    this.status = { phase: "idle", progress: 0, message: "尚未安装本机服务", logs: [], installed: false, ready: false, supported: false, server_base: LOCAL_SERVER_BASE };
  }

  async initialize() {
    this.status.supported = await localPayloadSupported(this.resources);
    this.status.installed = Boolean(await activeRelease(this.root));
    this.status.logs = await tail(join(this.root, "logs", "installer.log"));
    return this;
  }

  async isReady() {
    return probe(this.http, LOCAL_SERVER_BASE, this.serviceName);
  }

  async snapshot() {
    this.status.installed = Boolean(await activeRelease(this.root));
    this.status.supported = await localPayloadSupported(this.resources);
    if (await this.isReady()) {
      Object.assign(this.status, { phase: "ready", progress: 100, message: "本机服务已就绪", ready: true });
    }
    return structuredClone(this.status);
  }

  setStatus(phase, progress, message) {
    Object.assign(this.status, { phase, progress: Math.max(0, Math.min(100, progress)), message, ready: phase === "ready" });
    this.emit("status", this.status);
  }

  async log(message) {
    const line = String(message).trim();
    if (!line) return;
    const logDir = join(this.root, "logs");
    await mkdir(logDir, { recursive: true });
    const path = join(logDir, "installer.log");
    await appendFile(path, `${line}\n`, { encoding: "utf8", mode: 0o600 });
    this.status.logs = [...this.status.logs, line].slice(-80);
  }

  prepare() {
    if (this.installing || this.shuttingDown) return false;
    this.installing = true;
    void this.#prepare().finally(() => { this.installing = false; });
    return true;
  }

  async #prepare() {
    try {
      if (!this.status.supported) throw new Error("安装包未携带 UOS 1070 aarch64 本机运行时；请安装完整包");
      if (await needsInstall(this.root, this.resources)) await this.#install();
      else await this.start();
    } catch (error) {
      await this.log(`安装失败：${error.message}`);
      this.setStatus("error", 0, error.message);
    }
  }

  async #install() {
    const upgrading = Boolean(await activeRelease(this.root));
    await this.stop();
    const backup = upgrading ? await backupData(this.dataRoot, join(this.root, "backups")) : null;
    this.status.logs = [];
    await mkdir(join(this.root, "logs"), { recursive: true });
    await writeFile(join(this.root, "logs", "installer.log"), "");
    await this.log("开始离线安装本机服务；不会下载 Python 或项目依赖。");
    this.setStatus("installing", 2, "正在准备本机服务…");
    await installPayloads(this.root, this.resources, (progress, message) => {
      this.setStatus("installing", progress, message);
      void this.log(`HUGAGENT_PROGRESS|${progress}|${message}`);
    });
    try {
      await this.start();
      await pruneOld(this.root);
      await pruneBackups(join(this.root, "backups"));
    } catch (startError) {
      if (await restorePrevious(this.root)) {
        if (backup) await restoreData(this.dataRoot, backup);
        await this.log(`新版本启动失败，已回滚原有版本：${startError.message}`);
        await this.start();
        throw new Error(`新版本启动失败，已自动恢复原有版本：${startError.message}`);
      }
      throw startError;
    }
  }

  async start() {
    if (this.shuttingDown) throw new Error("桌面端正在退出，不再启动本机服务");
    if (await this.isReady()) {
      this.setStatus("ready", 100, "本机服务已就绪");
      return;
    }
    const release = await activeRelease(this.root);
    if (!release) throw new Error("本机服务尚未安装或版本状态无效");
    await mkdir(join(this.root, "logs"), { recursive: true });
    const logFd = openSync(join(this.root, "logs", "server.log"), "a", 0o600);
    const cli = join(release.sourceDir, "src", "backend", "cli.py");
    const nodeModules = join(this.root, "tools", "node", "node_modules");
    const env = {
      ...process.env,
      HUGAGENT_HOME: this.dataRoot,
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
      PYTHONDONTWRITEBYTECODE: "1",
      HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS: "1",
      FRONTEND_DIST_DIR: join(release.sourceDir, "src", "frontend", "dist"),
      NODE_PATH: nodeModules,
      PLAYWRIGHT_BROWSERS_PATH: join(this.root, "tools", "node", "browsers"),
    };
    if (this.bridgeSecret) {
      env.HUGAGENT_DESKTOP_BRIDGE_SECRET = this.bridgeSecret;
      env.CONFIG_TOKEN = this.bridgeSecret;
    }
    this.child = spawn(release.executable, [cli, "serve", "--host", "127.0.0.1", "--port", "32101", "--no-browser"], {
      cwd: release.sourceDir,
      env,
      detached: true,
      stdio: ["ignore", logFd, logFd],
    });
    closeSync(logFd);
    await writeFile(join(this.root, "server.pid"), String(this.child.pid), { mode: 0o600 });
    this.child.once("exit", () => { this.child = null; });
    this.setStatus("starting", 92, "正在启动本机服务…");
    for (let attempt = 0; attempt < 90; attempt += 1) {
      if (await this.isReady()) {
        this.setStatus("ready", 100, "本机服务已就绪");
        return;
      }
      if (!this.child) throw new Error(`本机服务提前退出，请查看 ${join(this.root, "logs", "server.log")}`);
      this.setStatus("starting", Math.min(99, 92 + Math.floor(attempt / 12)), "正在等待本机服务通过健康检查…");
      await new Promise((resolveWait) => setTimeout(resolveWait, 1_000));
    }
    await this.stop();
    throw new Error("本机服务启动超时，请查看日志后重试");
  }

  async stop() {
    const pid = this.child?.pid || Number((await readFile(join(this.root, "server.pid"), "utf8").catch(() => "0")).trim());
    if (!pid) return;
    if (!this.child) {
      const command = (await readFile(`/proc/${pid}/cmdline`, "utf8").catch(() => "")).replaceAll("\0", " ");
      if (!command.includes("src/backend/cli.py") || !command.includes(this.root)) {
        await rm(join(this.root, "server.pid"), { force: true });
        return;
      }
    }
    try { process.kill(-pid, "SIGTERM"); } catch (error) { if (error.code !== "ESRCH") throw error; }
    await new Promise((resolveWait) => setTimeout(resolveWait, 800));
    try { process.kill(-pid, "SIGKILL"); } catch (error) { if (error.code !== "ESRCH") throw error; }
    this.child = null;
    await rm(join(this.root, "server.pid"), { force: true });
  }

  async shutdown() {
    this.shuttingDown = true;
    await this.stop();
  }
}

export { backupData, restoreData };
