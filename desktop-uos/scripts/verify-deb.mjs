import { execFileSync } from "node:child_process";
import { mkdtempSync, readdirSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const desktopDir = resolve(import.meta.dirname, "..");
const deb = process.argv[2] || readdirSync(join(desktopDir, "dist")).filter((name) => name.endsWith(".deb")).map((name) => join(desktopDir, "dist", name))[0];
if (!deb || !statSync(deb).isFile()) throw new Error("请传入要检查的 .deb 路径");

const info = execFileSync("dpkg-deb", ["--info", deb], { encoding: "utf8" });
if (!/^ Architecture: arm64$/m.test(info)) throw new Error("deb 不是 arm64 架构");
if (!/libc6 \(>= 2\.28\)/.test(info)) throw new Error("deb 未声明 UOS 1070 glibc 2.28 基线");

const extracted = mkdtempSync(join(tmpdir(), "hugagent-uos-deb-"));
try {
  execFileSync("dpkg-deb", ["--extract", deb, extracted]);
  const binary = join(extracted, "opt", "HugAgentOS UOS", "hugagent-uos");
  const kind = execFileSync("file", ["-b", binary], { encoding: "utf8" });
  if (!/ARM aarch64|ARM64/i.test(kind)) throw new Error(`Electron 主程序不是 aarch64：${kind.trim()}`);
  const versions = execFileSync("readelf", ["--version-info", binary], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 })
    .match(/GLIBC_(\d+)\.(\d+)/g) || [];
  const highest = versions
    .map((value) => value.slice(6).split(".").map(Number))
    .sort((a, b) => b[0] - a[0] || b[1] - a[1])[0] || [0, 0];
  if (highest[0] > 2 || (highest[0] === 2 && highest[1] > 28)) throw new Error(`Electron 主程序需要 GLIBC_${highest.join(".")}，高于 UOS 1070`);
  for (const required of [
    join(extracted, "opt", "HugAgentOS UOS", "resources", "web", "index.html"),
    join(extracted, "opt", "HugAgentOS UOS", "resources", "bundle-flavor.json"),
  ]) if (!statSync(required).isFile()) throw new Error(`deb 缺少资源：${required}`);
  console.log(`[desktop-uos] verified ${deb}`);
  console.log(`[desktop-uos] Architecture=arm64, max GLIBC=${highest.join(".")}`);
} finally {
  rmSync(extracted, { recursive: true, force: true });
}
