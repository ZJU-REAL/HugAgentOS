import { spawnSync } from "node:child_process";
import {
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  DESKTOP_LOCK_INPUT_MARKER,
  DESKTOP_REQUIREMENTS_FILE,
  DESKTOP_TARGETS,
  desktopTargetConfig,
  desktopTargetInputHash,
} from "./desktop-dependencies.mjs";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(desktopDir, "..");
const requested = process.argv.slice(2);
const targets =
  requested.length === 0 || requested.includes("all")
    ? Object.keys(DESKTOP_TARGETS)
    : requested;

for (const target of targets) generateLock(target);

function generateLock(target) {
  const config = desktopTargetConfig(target);
  const output = join(repoRoot, config.lockFile);
  const temporary = `${output}.tmp`;
  const args = [
    "pip",
    "compile",
    DESKTOP_REQUIREMENTS_FILE,
    "--python",
    "3.11",
    "--python-version",
    "3.11",
    "--python-platform",
    config.pythonPlatform,
    "--only-binary",
    ":all:",
    "--no-header",
    "--output-file",
    temporary,
  ];
  if (config.overridesFile) {
    args.splice(3, 0, "--overrides", config.overridesFile);
  }

  rmSync(temporary, { force: true });
  const result = spawnSync("uv", args, {
    cwd: repoRoot,
    encoding: "utf8",
    shell: false,
    stdio: ["ignore", "inherit", "inherit"],
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    rmSync(temporary, { force: true });
    throw new Error(`uv pip compile failed for ${target}: ${result.status}`);
  }

  const compiled = readFileSync(temporary, "utf8").replace(/\r\n/g, "\n");
  const inputHash = desktopTargetInputHash(repoRoot, target);
  const regenerate =
    target === "windows-x86_64"
      ? "npm --prefix desktop run lock:windows"
      : "npm --prefix desktop run lock:desktop";
  const header = [
    "# This file is generated. Do not edit it by hand.",
    `# Regenerate with: ${regenerate}`,
    `${DESKTOP_LOCK_INPUT_MARKER}${inputHash}`,
    `# Target: ${config.label}`,
    "",
  ].join("\n");
  writeFileSync(temporary, `${header}${compiled}`, "utf8");
  renameSync(temporary, output);
  console.log(`Wrote ${config.lockFile}`);
}
