// Small process and file helpers shared by the desktop build scripts
// (desktop/scripts and desktop-uos/scripts).
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

/** Run a command with inherited stdio; throw when it cannot start or exits non-zero. */
export function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd,
    env: { ...process.env, ...(options.env || {}) },
    stdio: "inherit",
    shell: false,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(" ")} failed with exit code ${result.status}`);
  }
}

/**
 * Find a host Python 3.11.4+ whose tarfile supports extraction filters.
 * `purpose` completes the error message ("... is required to <purpose>.").
 */
export function findPython(purpose) {
  const commands = process.platform === "win32" ? ["python", "py"] : ["python3", "python"];
  for (const command of commands) {
    const prefix = command === "py" ? ["-3"] : [];
    const result = spawnSync(
      command,
      [
        ...prefix,
        "-c",
        "import sys, tarfile; raise SystemExit(0 if sys.version_info >= (3, 11) and hasattr(tarfile, 'data_filter') else 1)",
      ],
      { stdio: "ignore", shell: false },
    );
    if (result.status === 0) return { command, prefix };
  }
  throw new Error(`Python 3.11.4 or later with tarfile extraction filters is required to ${purpose}.`);
}

/** SHA-256 hex digest of a file's bytes. */
export function hashFile(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}
