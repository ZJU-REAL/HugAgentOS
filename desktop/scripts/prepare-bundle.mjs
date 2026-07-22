import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { stageTrackedCeRepository } from "./ce-payload.mjs";
import { readDesktopVersion } from "./desktop-version.mjs";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(desktopDir, "..");
const generatedRoot = join(desktopDir, "generated", "server-ce");
const npmCommand =
  process.platform === "win32" ? process.env.ComSpec || "cmd.exe" : "npm";
const npmPrefix =
  process.platform === "win32" ? ["/d", "/s", "/c", "npm.cmd"] : [];
const desktopVersion = readDesktopVersion(desktopDir);

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd || repoRoot,
    env: { ...process.env, ...(options.env || {}) },
    stdio: "inherit",
    shell: false,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(
      `${command} ${args.join(" ")} failed with exit code ${result.status}`,
    );
  }
}

function findPython() {
  const commands =
    process.platform === "win32" ? ["python", "py"] : ["python3", "python"];
  for (const command of commands) {
    const prefix = command === "py" ? ["-3"] : [];
    const result = spawnSync(
      command,
      [
        ...prefix,
        "-c",
        "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)",
      ],
      {
        stdio: "ignore",
        shell: false,
      },
    );
    if (result.status === 0) return { command, prefix };
  }
  throw new Error(
    "Python 3.11 or later is required to generate the bundled CE server.",
  );
}

console.log("[desktop] Building the desktop web application");
run(npmCommand, [...npmPrefix, "run", "build"], {
  cwd: join(repoRoot, "src", "frontend"),
});

const ceBuilder = join(repoRoot, "scripts", "build_ce.py");
const requireClean =
  Boolean(process.env.CI) || process.env.HUGAGENT_RELEASE_BUILD === "1";
if (existsSync(ceBuilder)) {
  const python = findPython();
  const ceArgs = [...python.prefix, ceBuilder, "--out", generatedRoot];
  if (!requireClean) ceArgs.push("--allow-dirty");

  console.log(
    "[desktop] Generating the CE server payload from the source checkout",
  );
  rmSync(generatedRoot, {
    recursive: true,
    force: true,
    maxRetries: 3,
    retryDelay: 200,
  });
  run(python.command, ceArgs, {
    env: {
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
    },
  });
} else {
  console.log(
    "[desktop] CE generator not present; staging the current derived CE repository",
  );
  const copied = stageTrackedCeRepository(repoRoot, generatedRoot, {
    requireClean,
  });
  console.log(`[desktop] Staged ${copied} tracked CE files`);
}

console.log("[desktop] Building the CE login/onboarding web application");
run(
  npmCommand,
  [...npmPrefix, "install", "--no-audit", "--no-fund", "--no-package-lock"],
  {
    cwd: join(generatedRoot, "src", "frontend"),
  },
);
run(npmCommand, [...npmPrefix, "run", "build"], {
  cwd: join(generatedRoot, "src", "frontend"),
  env: {
    VITE_EDITION: "ce",
    VITE_DEFAULT_LANGUAGE: "zh-CN",
    NODE_OPTIONS: process.env.NODE_OPTIONS || "--max-old-space-size=6144",
  },
});
// node_modules is a build-time input, not a server runtime dependency. Keeping it
// would inflate the NSIS payload by hundreds of megabytes.
rmSync(join(generatedRoot, "src", "frontend", "node_modules"), {
  recursive: true,
  force: true,
});

const revision = spawnSync("git", ["rev-parse", "HEAD"], {
  cwd: repoRoot,
  encoding: "utf8",
  shell: false,
});
if (revision.status !== 0)
  throw new Error("Unable to resolve the Git revision for the bundle.");
mkdirSync(generatedRoot, { recursive: true });
writeFileSync(
  join(generatedRoot, "desktop-bundle.json"),
  `${JSON.stringify(
    {
      schema: 1,
      desktop_version: desktopVersion,
      source_revision: revision.stdout.trim(),
    },
    null,
    2,
  )}\n`,
  "utf8",
);

console.log(`[desktop] Local server payload ready: ${generatedRoot}`);
