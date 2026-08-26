import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import {
  access,
  copyFile,
  mkdir,
  readFile,
  readdir,
  rename,
  rm,
  stat,
  statfs,
} from "node:fs/promises";
import { dirname, isAbsolute, join, normalize, relative, resolve, sep } from "node:path";
import { spawn } from "node:child_process";
import extractZip from "extract-zip";
import * as tar from "tar";
import { TARGET } from "./config.mjs";
import { readJson, writeJson } from "./storage.mjs";

export function safeRelative(value, label = "路径") {
  const raw = String(value || "");
  const normalized = normalize(raw);
  if (
    !raw ||
    isAbsolute(raw) ||
    normalized === ".." ||
    normalized.startsWith(`..${sep}`) ||
    /^[a-zA-Z]:/.test(raw) ||
    raw.includes("\0")
  ) throw new Error(`${label}不是安全的相对路径`);
  return normalized;
}

export function safeLink(entryPath, linkPath) {
  const parent = dirname(safeRelative(entryPath, "符号链接路径"));
  const resolved = normalize(join(parent, String(linkPath || "")));
  safeRelative(resolved, "符号链接目标");
  return true;
}

export function validIdentifier(value) {
  return typeof value === "string" && value.length >= 16 && /^[a-f0-9]+$/i.test(value);
}

export async function hashFile(path) {
  const digest = createHash("sha256");
  await new Promise((resolveHash, reject) => {
    const stream = createReadStream(path);
    stream.on("data", (chunk) => digest.update(chunk));
    stream.on("error", reject);
    stream.on("end", resolveHash);
  });
  return digest.digest("hex");
}

function hashBytes(value) {
  return createHash("sha256").update(value).digest("hex");
}

async function exists(path) {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

function roots(root) {
  return {
    sources: join(root, "releases", "sources"),
    runtimes: join(root, "releases", "runtimes"),
    active: join(root, "active.json"),
    previous: join(root, "previous.json"),
  };
}

export async function resolveRelease(root, release) {
  if (!release || !validIdentifier(release.source_id) || !validIdentifier(release.runtime_id)) return null;
  const paths = roots(root);
  const sourceDir = join(paths.sources, release.source_id);
  const runtimeDir = join(paths.runtimes, release.runtime_id);
  const executable = join(runtimeDir, safeRelative(release.executable, "Python 解释器"));
  const smokeTest = join(runtimeDir, safeRelative(release.smoke_test, "运行时自检"));
  for (const required of [
    join(sourceDir, "src/backend/cli.py"),
    join(sourceDir, "src/frontend/dist/index.html"),
    executable,
    smokeTest,
  ]) if (!await exists(required)) return null;
  return { release, sourceDir, runtimeDir, executable, smokeTest };
}

export async function activeRelease(root) {
  return resolveRelease(root, await readJson(roots(root).active));
}

async function loadManifests(resources) {
  const serverRaw = await readFile(resources.serverManifest);
  const server = JSON.parse(serverRaw.toString("utf8"));
  const runtime = JSON.parse(await readFile(resources.runtimeManifest, "utf8"));
  return { server, serverRaw, runtime };
}

export async function localPayloadSupported(resources) {
  try {
    const { server, runtime } = await loadManifests(resources);
    return server.schema === 2 && runtime.schema === 1 && server.target === TARGET && runtime.target === TARGET;
  } catch {
    return false;
  }
}

export async function needsInstall(root, resources) {
  try {
    const { server, serverRaw, runtime } = await loadManifests(resources);
    const active = await activeRelease(root);
    return !active ||
      active.release.source_id !== hashBytes(serverRaw) ||
      active.release.runtime_id !== runtime.dependency_fingerprint ||
      server.dependency_fingerprint !== runtime.dependency_fingerprint ||
      active.release.target !== TARGET;
  } catch {
    return true;
  }
}

function validateManifests(server, runtime) {
  if (server.schema !== 2 || runtime.schema !== 1) throw new Error("本机服务资源清单版本不受支持");
  if (server.target !== TARGET || runtime.target !== TARGET) {
    throw new Error(`安装包平台不匹配：需要 ${TARGET}，服务=${server.target}，运行时=${runtime.target}`);
  }
  if (!validIdentifier(runtime.dependency_fingerprint)) throw new Error("运行时依赖指纹无效");
  if (server.dependency_fingerprint !== runtime.dependency_fingerprint) throw new Error("服务资源与 Python 运行时依赖指纹不一致");
  if (runtime.archive !== "runtime-core.tar.gz") throw new Error("运行时清单引用了未知归档");
  safeRelative(runtime.executable, "Python 解释器");
  safeRelative(runtime.smoke_test, "运行时自检");
}

async function ensureSpace(root, resources, runtime) {
  const disk = await statfs(root);
  const available = Number(disk.bavail) * Number(disk.bsize);
  const sourceSize = Number((await stat(resources.serverArchive)).size);
  const required = Number(runtime.unpacked_size || 0) + Number(runtime.archive_size || 0) + sourceSize * 3 + 256 * 1024 * 1024;
  if (available < required) {
    throw new Error(`磁盘空间不足：至少需 ${(required / 1073741824).toFixed(1)} GB，当前 ${(available / 1073741824).toFixed(1)} GB`);
  }
}

async function validateSource(root, expectedManifest) {
  for (const item of ["desktop-bundle.json", "pyproject.toml", "src/backend/cli.py", "src/frontend/dist/index.html"]) {
    if (!await exists(join(root, item))) throw new Error(`本机服务资源缺少 ${item}`);
  }
  const actual = await readFile(join(root, "desktop-bundle.json"));
  if (!actual.equals(expectedManifest)) throw new Error("解压后的服务清单与安装包不一致");
}

async function validateRuntime(root, expected) {
  const layout = await readJson(join(root, "runtime-layout.json"));
  for (const key of ["schema", "target", "python_version", "dependency_fingerprint", "executable", "smoke_test"]) {
    if (layout?.[key] !== expected[key]) throw new Error("解压后的 Python 运行时与外部清单不一致");
  }
  if (!await exists(join(root, safeRelative(layout.executable))) || !await exists(join(root, safeRelative(layout.smoke_test)))) {
    throw new Error("Python 运行时缺少解释器或自检脚本");
  }
}

async function extractServer(archive, destination) {
  await extractZip(archive, {
    dir: destination,
    onEntry(entry) {
      safeRelative(entry.fileName, "服务归档路径");
      const mode = (entry.externalFileAttributes >>> 16) & 0o170000;
      if (mode === 0o120000) throw new Error(`服务归档不允许符号链接：${entry.fileName}`);
    },
  });
}

async function extractRuntime(archive, destination) {
  await tar.x({
    file: archive,
    cwd: destination,
    strict: true,
    preservePaths: false,
    filter(path, entry) {
      safeRelative(path, "运行时归档路径");
      if (entry.type === "SymbolicLink" || entry.type === "Link") safeLink(path, entry.linkpath);
      if (!["File", "OldFile", "Directory", "SymbolicLink", "Link"].includes(entry.type)) {
        throw new Error(`运行时包含不支持的归档条目：${path}`);
      }
      return true;
    },
  });
}

async function runSmoke(release) {
  await new Promise((resolveRun, reject) => {
    const child = spawn(release.executable, [release.smokeTest, "--source", release.sourceDir], {
      cwd: release.sourceDir,
      env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", PYTHONDONTWRITEBYTECODE: "1" },
      stdio: ["ignore", "ignore", "pipe"],
    });
    let stderr = "";
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", reject);
    child.on("exit", (code) => code === 0 ? resolveRun() : reject(new Error(`离线 Python 自检失败：${stderr.trim()}`)));
  });
}

async function commit(staged, destination) {
  if (await exists(destination)) return rm(staged, { recursive: true, force: true });
  await rename(staged, destination);
}

async function stageDirectory(parent, id, unpack, validate) {
  const destination = join(parent, id);
  if (await exists(destination)) {
    await validate(destination);
    return destination;
  }
  const staged = join(parent, `.${id}.stage-${Date.now()}-${process.pid}`);
  await rm(staged, { recursive: true, force: true });
  await mkdir(staged, { recursive: true });
  try {
    await unpack(staged);
    await validate(staged);
    await commit(staged, destination);
    return destination;
  } catch (error) {
    await rm(staged, { recursive: true, force: true });
    throw error;
  }
}

export async function installPayloads(root, resources, progress = () => {}) {
  await mkdir(root, { recursive: true, mode: 0o700 });
  const { server, serverRaw, runtime } = await loadManifests(resources);
  validateManifests(server, runtime);
  const archiveHash = await hashFile(resources.runtimeArchive);
  if (archiveHash.toLowerCase() !== String(runtime.archive_sha256).toLowerCase()) throw new Error("离线 Python 运行时校验失败，请重新下载安装包");
  await ensureSpace(root, resources, runtime);
  const paths = roots(root);
  await mkdir(paths.sources, { recursive: true });
  await mkdir(paths.runtimes, { recursive: true });
  const sourceId = hashBytes(serverRaw);
  const runtimeId = runtime.dependency_fingerprint;
  progress(8, "正在解压同版本服务资源…");
  await stageDirectory(paths.sources, sourceId, (dest) => extractServer(resources.serverArchive, dest), (dest) => validateSource(dest, serverRaw));
  progress(35, "正在解压离线 Python 运行环境…");
  await stageDirectory(paths.runtimes, runtimeId, (dest) => extractRuntime(resources.runtimeArchive, dest), (dest) => validateRuntime(dest, runtime));
  const releaseState = {
    schema: 1,
    desktop_version: server.desktop_version,
    source_revision: server.source_revision,
    source_id: sourceId,
    runtime_id: runtimeId,
    target: TARGET,
    executable: runtime.executable,
    smoke_test: runtime.smoke_test,
  };
  const release = await resolveRelease(root, releaseState);
  if (!release) throw new Error("无法解析已安装的本机服务版本");
  progress(82, "正在验证本机服务运行环境…");
  await runSmoke(release);
  if (await exists(paths.active)) await copyFile(paths.active, paths.previous);
  await writeJson(paths.active, releaseState);
  progress(90, "离线本机服务已安装，正在启动…");
  return release;
}

export async function restorePrevious(root) {
  const paths = roots(root);
  const previous = await readJson(paths.previous);
  if (!await resolveRelease(root, previous)) return false;
  const current = await readFile(paths.active).catch(() => null);
  await writeJson(paths.active, previous);
  if (current) await writeJson(paths.previous, JSON.parse(current.toString("utf8")));
  return true;
}

export async function pruneOld(root) {
  const paths = roots(root);
  const keepSources = new Set();
  const keepRuntimes = new Set();
  for (const path of [paths.active, paths.previous]) {
    const release = await readJson(path);
    if (validIdentifier(release?.source_id)) keepSources.add(release.source_id);
    if (validIdentifier(release?.runtime_id)) keepRuntimes.add(release.runtime_id);
  }
  for (const [parent, keep] of [[paths.sources, keepSources], [paths.runtimes, keepRuntimes]]) {
    for (const entry of await readdir(parent, { withFileTypes: true }).catch(() => [])) {
      if (!entry.name.startsWith(".") && !keep.has(entry.name)) await rm(join(parent, entry.name), { recursive: true, force: true });
    }
  }
}
