import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { buildDesktopRuntime } from "./build-runtime.mjs";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(desktopDir, "..");
const sourceRoot = join(desktopDir, "generated", "server-ce");
const python = findPython();

buildDesktopRuntime({ desktopDir, repoRoot, sourceRoot, python });

function findPython() {
  for (const command of process.platform === "win32" ? ["python", "py"] : ["python3", "python"]) {
    const prefix = command === "py" ? ["-3"] : [];
    const result = spawnSync(command, [...prefix, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"], {
      stdio: "ignore",
      shell: false,
    });
    if (result.status === 0) return { command, prefix };
  }
  throw new Error("Python 3.11 or later is required to archive the offline runtime.");
}
