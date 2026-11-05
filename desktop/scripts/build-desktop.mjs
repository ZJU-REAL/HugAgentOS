import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { assertMacosSigning } from "./macos-signing.mjs";
import { explicitTauriTarget, validateDesktopBuildTarget } from "./desktop-build-target.mjs";
import { resolveHybridOnly } from "./desktop-hybrid-only.mjs";
import { withParkedFullBundle, writeUpdateBundleInputs } from "./update-bundle.mjs";

assertMacosSigning();

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const flavor = process.argv[2] || "full";
if (!["full", "thin", "update"].includes(flavor)) {
  throw new Error(`Unknown desktop bundle flavor: ${flavor}`);
}
const rawArgs = process.argv.slice(3);
const hybridOnly = resolveHybridOnly(rawArgs, process.env);
const tauriArgs = rawArgs.filter((arg) => arg !== "--hybrid-only");
validateDesktopBuildTarget(tauriArgs);

const cli = join(desktopDir, "node_modules", "@tauri-apps", "cli", "tauri.js");
function tauriBuild(args) {
  const result = spawnSync(process.execPath, [cli, "build", ...args], {
    cwd: desktopDir,
    env: {
      ...process.env,
      // The update package is the full binary with an empty runtime placeholder.
      HUGAGENT_DESKTOP_BUNDLE: flavor === "update" ? "full" : flavor,
      ...(hybridOnly ? { JX_DESKTOP_HYBRID_ONLY: "1" } : {}),
    },
    stdio: "inherit",
    shell: false,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

if (flavor === "update") {
  const config = writeUpdateBundleInputs(desktopDir);
  const triple = explicitTauriTarget(tauriArgs);
  const releaseDir = join(desktopDir, "src-tauri", "target", ...(triple ? [triple] : []), "release");
  const output = withParkedFullBundle(releaseDir, () => tauriBuild([...tauriArgs, "--config", config]));
  console.log(`[desktop] Slim update package ready: ${output}`);
} else {
  tauriBuild(tauriArgs);
}
