import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { validateDesktopBuildTarget } from "./desktop-build-target.mjs";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const flavor = process.argv[2] || "full";
if (!["full", "thin"].includes(flavor)) {
  throw new Error(`Unknown desktop bundle flavor: ${flavor}`);
}
const tauriArgs = process.argv.slice(3);
validateDesktopBuildTarget(tauriArgs);

const cli = join(desktopDir, "node_modules", "@tauri-apps", "cli", "tauri.js");
const result = spawnSync(process.execPath, [cli, "build", ...tauriArgs], {
  cwd: desktopDir,
  env: { ...process.env, HUGAGENT_DESKTOP_BUNDLE: flavor },
  stdio: "inherit",
  shell: false,
});
if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status ?? 1);
