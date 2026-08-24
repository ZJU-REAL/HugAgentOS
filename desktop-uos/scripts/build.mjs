import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  copyFileSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(desktopDir, "..");
const generated = join(desktopDir, "generated");
const flavor = process.argv[2] || "full";
const directoryOnly = process.argv.includes("--dir");
if (!["full", "thin"].includes(flavor)) throw new Error(`Unknown bundle flavor: ${flavor}`);

const [nodeMajor, nodeMinor] = process.versions.node.split(".").map(Number);
if (nodeMajor < 22 || (nodeMajor === 22 && nodeMinor < 12)) {
  throw new Error(`Electron 43 构建需要 Node >=22.12，当前 ${process.version}`);
}

const packageJson = JSON.parse(readFileSync(join(desktopDir, "package.json"), "utf8"));
const tauriPackage = JSON.parse(readFileSync(join(repoRoot, "desktop", "package.json"), "utf8"));
if (packageJson.version !== tauriPackage.version) {
  throw new Error(`桌面版本不一致：desktop-uos=${packageJson.version}, desktop=${tauriPackage.version}`);
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd || repoRoot,
    env: { ...process.env, ...(options.env || {}) },
    stdio: "inherit",
    shell: false,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${command} ${args.join(" ")} failed (${result.status})`);
}

mkdirSync(generated, { recursive: true });
writeFileSync(join(generated, "brand.json"), `${JSON.stringify({
  name: process.env.JX_BRAND_NAME || "HugAgentOS",
  website_url: process.env.JX_BRAND_WEBSITE_URL || "",
  local_service_name: process.env.JX_LOCAL_SERVICE_NAME || "hugagent",
}, null, 2)}\n`);

if (flavor === "full") {
  if (process.platform !== "linux" || process.arch !== "arm64") {
    throw new Error(
      "UOS 完整包必须在原生 Linux aarch64 构建机上生成，避免把 x86_64 或高 glibc Python 运行时装进 ARM64 包。" +
      "当前机器可运行 npm run build:thin 验证 Electron ARM64 外壳。",
    );
  }
  run(process.execPath, [join(repoRoot, "desktop", "scripts", "prepare-bundle.mjs")], {
    env: { HUGAGENT_DESKTOP_BUNDLE: "full" },
  });
  for (const [source, target] of [
    ["desktop/generated/server-ce.zip", "server-ce.zip"],
    ["desktop/generated/server-ce/desktop-bundle.json", "server-ce-manifest.json"],
    ["desktop/generated/runtime-core.tar.gz", "runtime-core.tar.gz"],
    ["desktop/generated/runtime-manifest.json", "runtime-manifest.json"],
  ]) copyFileSync(join(repoRoot, source), join(generated, target));
} else {
  run("npm", ["run", "build"], { cwd: join(repoRoot, "src", "frontend") });
  const placeholder = `${JSON.stringify({ schema: 0, flavor: "thin", desktop_version: packageJson.version, target: "linux-aarch64" }, null, 2)}\n`;
  writeFileSync(join(generated, "server-ce.zip"), "");
  writeFileSync(join(generated, "server-ce-manifest.json"), placeholder);
  writeFileSync(join(generated, "runtime-core.tar.gz"), "");
  writeFileSync(join(generated, "runtime-manifest.json"), placeholder);
}
writeFileSync(join(generated, "bundle-flavor.json"), `${JSON.stringify({ schema: 1, flavor, target: "linux-aarch64" }, null, 2)}\n`);

rmSync(join(desktopDir, "dist"), { recursive: true, force: true });
const builder = join(desktopDir, "node_modules", "electron-builder", "out", "cli", "cli.js");
run(process.execPath, [builder, "--linux", directoryOnly ? "dir" : "deb", "--arm64", "--publish", "never"], { cwd: desktopDir });

if (!directoryOnly) {
  const deb = readdirSync(join(desktopDir, "dist"))
    .filter((name) => name.endsWith(".deb"))
    .map((name) => join(desktopDir, "dist", name))[0];
  if (!deb) throw new Error("electron-builder 未生成 .deb");
  const hash = createHash("sha256").update(readFileSync(deb)).digest("hex");
  const manifest = {
    version: packageJson.version,
    notes: "HugAgentOS UOS 1070 aarch64 Electron 43 desktop release",
    pub_date: new Date().toISOString(),
    platforms: {
      "linux-aarch64": {
        url: deb.split(/[\\/]/).at(-1),
        sha256: hash,
        size: statSync(deb).size,
        format: "deb",
        runtime: flavor,
      },
    },
  };
  writeFileSync(join(desktopDir, "dist", "latest-uos.json"), `${JSON.stringify(manifest, null, 2)}\n`);
  console.log(`[desktop-uos] ${flavor} ARM64 deb ready: ${deb}`);
}
