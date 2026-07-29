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
  DESKTOP_REQUIREMENTS_FILE,
  WINDOWS_DESKTOP_LOCK_FILE,
  WINDOWS_LOCK_INPUT_MARKER,
  desktopRequirementsInputHash,
} from "./desktop-dependencies.mjs";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(desktopDir, "..");
const output = join(repoRoot, WINDOWS_DESKTOP_LOCK_FILE);
const temporary = `${output}.tmp`;

rmSync(temporary, { force: true });
const result = spawnSync(
  "uv",
  [
    "pip",
    "compile",
    DESKTOP_REQUIREMENTS_FILE,
    "--python",
    "3.11",
    "--python-version",
    "3.11",
    "--python-platform",
    "x86_64-pc-windows-msvc",
    "--only-binary",
    ":all:",
    "--no-header",
    "--output-file",
    temporary,
  ],
  {
    cwd: repoRoot,
    encoding: "utf8",
    shell: false,
    stdio: ["ignore", "inherit", "inherit"],
  },
);
if (result.error) throw result.error;
if (result.status !== 0) {
  rmSync(temporary, { force: true });
  throw new Error(`uv pip compile failed with exit code ${result.status}`);
}

const compiled = readFileSync(temporary, "utf8").replace(/\r\n/g, "\n");
const inputHash = desktopRequirementsInputHash(repoRoot);
const header = [
  "# This file is generated. Do not edit it by hand.",
  "# Regenerate with: npm --prefix desktop run lock:windows",
  `${WINDOWS_LOCK_INPUT_MARKER}${inputHash}`,
  "# Target: CPython 3.11 on Windows x86_64",
  "",
].join("\n");
writeFileSync(temporary, `${header}${compiled}`, "utf8");
renameSync(temporary, output);
console.log(`Wrote ${WINDOWS_DESKTOP_LOCK_FILE}`);
