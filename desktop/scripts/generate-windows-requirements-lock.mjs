import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const scriptsDir = dirname(fileURLToPath(import.meta.url));
const result = spawnSync(
  process.execPath,
  [
    join(scriptsDir, "generate-platform-requirements-locks.mjs"),
    "windows-x86_64",
  ],
  { stdio: "inherit", shell: false },
);
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
