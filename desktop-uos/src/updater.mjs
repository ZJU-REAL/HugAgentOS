import { createHash } from "node:crypto";
import { createWriteStream } from "node:fs";
import { access, mkdir, rm } from "node:fs/promises";
import httpModule from "node:http";
import httpsModule from "node:https";
import { basename, join } from "node:path";
import { spawn } from "node:child_process";

export function compareVersions(left, right) {
  const a = String(left).replace(/^v/, "").split(".").map((v) => Number.parseInt(v, 10) || 0);
  const b = String(right).replace(/^v/, "").split(".").map((v) => Number.parseInt(v, 10) || 0);
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    if ((a[index] || 0) !== (b[index] || 0)) return (a[index] || 0) > (b[index] || 0) ? 1 : -1;
  }
  return 0;
}

export function selectUosRelease(manifest) {
  const platforms = manifest?.platforms || {};
  return platforms["linux-aarch64"] || platforms["linux-arm64"] || platforms["electron-linux-arm64"] || null;
}

async function download(url, destination, expectedHash, httpClient, onProgress) {
  const parsed = new URL(url);
  const transport = parsed.protocol === "https:" ? httpsModule : httpModule;
  await mkdir(join(destination, ".."), { recursive: true });
  await new Promise((resolveDownload, reject) => {
    const request = transport.get(parsed, { agent: httpClient.agentFor(parsed) }, (response) => {
      if ((response.statusCode || 0) < 200 || response.statusCode >= 300) {
        response.resume();
        reject(new Error(`下载安装包失败: HTTP ${response.statusCode}`));
        return;
      }
      const output = createWriteStream(destination, { mode: 0o600 });
      const hash = createHash("sha256");
      const total = Number(response.headers["content-length"] || 0);
      let received = 0;
      response.on("data", (chunk) => { hash.update(chunk); received += chunk.length; onProgress?.(received, total); });
      response.pipe(output);
      output.on("finish", () => {
        output.close(() => {
          const actual = hash.digest("hex");
          if (actual.toLowerCase() !== expectedHash.toLowerCase()) reject(new Error("更新包 SHA-256 校验失败"));
          else resolveDownload();
        });
      });
      output.on("error", reject);
    });
    request.on("error", reject);
  });
}

async function runPkexec(packagePath) {
  await access("/usr/bin/pkexec");
  await new Promise((resolveRun, reject) => {
    const child = spawn("/usr/bin/pkexec", ["/usr/bin/dpkg", "-i", packagePath], { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", reject);
    child.on("exit", (code) => code === 0 ? resolveRun() : reject(new Error(stderr.trim() || `dpkg 退出码 ${code}`)));
  });
}

export async function checkForUpdates({ app, dialog, shell, http, updateBase, silent = false }) {
  try {
    const response = await http.fetch(`${updateBase.replace(/\/$/, "")}/api/v1/desktop/latest.json`);
    if (response.status === 204) return;
    if (!response.ok) throw new Error(`更新清单 HTTP ${response.status}`);
    const manifest = await response.json();
    if (compareVersions(manifest.version, app.getVersion()) <= 0) {
      if (!silent) await dialog.showMessageBox({ type: "info", title: "HugAgentOS 更新", message: "当前已是最新版本。" });
      return;
    }
    const release = selectUosRelease(manifest);
    if (!release?.url || !/^[a-f0-9]{64}$/i.test(release.sha256 || "")) {
      throw new Error("更新清单缺少 linux-aarch64 的 URL 或 SHA-256，已拒绝不安全更新");
    }
    const confirm = await dialog.showMessageBox({
      type: "info",
      title: "HugAgentOS 更新",
      message: `发现新版本 ${manifest.version}`,
      detail: `${manifest.notes || ""}\n\n将下载并通过 UOS 系统授权安装 .deb。`,
      buttons: ["立即更新", "稍后"],
      defaultId: 0,
      cancelId: 1,
    });
    if (confirm.response !== 0) return;
    const filename = basename(new URL(release.url).pathname) || `hugagent-uos-${manifest.version}-arm64.deb`;
    if (!filename.endsWith(".deb")) throw new Error("UOS 更新包必须是 .deb");
    const destination = join(app.getPath("temp"), `hugagent-uos-update-${process.pid}`, filename);
    await rm(join(destination, ".."), { recursive: true, force: true });
    await download(release.url, destination, release.sha256, http);
    try {
      await runPkexec(destination);
      app.relaunch();
      app.quit();
    } catch (error) {
      shell.showItemInFolder(destination);
      throw new Error(`自动安装未完成：${error.message}\n已保留安装包，可手动安装。`);
    }
  } catch (error) {
    if (!silent) await dialog.showMessageBox({ type: "error", title: "HugAgentOS 更新", message: error.message });
    else console.warn(`[update] ${error.message}`);
  }
}
