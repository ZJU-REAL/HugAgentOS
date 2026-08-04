import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { stageTrackedCeRepository } from "./ce-payload.mjs";
import {
  currentDesktopTarget,
  desktopDependencyFingerprint,
} from "./desktop-dependencies.mjs";
import { readDesktopVersion } from "./desktop-version.mjs";
import { buildDesktopRuntime } from "./build-runtime.mjs";
import { validateDesktopBuildTarget } from "./desktop-build-target.mjs";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(desktopDir, "..");
const generatedRoot = join(desktopDir, "generated", "server-ce");
const generatedArchive = join(desktopDir, "generated", "server-ce.zip");
const npmCommand =
  process.platform === "win32" ? process.env.ComSpec || "cmd.exe" : "npm";
const npmPrefix =
  process.platform === "win32" ? ["/d", "/s", "/c", "npm.cmd"] : [];
const desktopVersion = readDesktopVersion(desktopDir);
const bundleFlavor = process.env.HUGAGENT_DESKTOP_BUNDLE || "full";
if (!["full", "thin"].includes(bundleFlavor)) {
  throw new Error(`Unknown HUGAGENT_DESKTOP_BUNDLE flavor: ${bundleFlavor}`);
}
if (bundleFlavor === "full") {
  // The offline runtime is a native payload, so a cross-architecture target
  // cannot be packaged with it. Thin bundles carry no runtime — any target,
  // including universal-apple-darwin, is buildable on any host.
  validateDesktopBuildTarget(
    process.env.TAURI_ENV_TARGET_TRIPLE
      ? ["--target", process.env.TAURI_ENV_TARGET_TRIPLE]
      : [],
  );
}
const desktopTarget = currentDesktopTarget();
const dependencyFingerprint = desktopDependencyFingerprint(repoRoot, desktopTarget);
const python = bundleFlavor === "full" ? findPython() : null;

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

function assertUv() {
  const result = spawnSync("uv", ["--version"], {
    stdio: "ignore",
    shell: false,
  });
  if (result.status !== 0) {
    throw new Error(
      "uv 0.11.33 is required on the release builder. End users do not need uv.",
    );
  }
}

if (bundleFlavor === "full") assertUv();

console.log("[desktop] Building the desktop web application");
run(npmCommand, [...npmPrefix, "run", "build"], {
  cwd: join(repoRoot, "src", "frontend"),
});

if (bundleFlavor === "thin") {
  console.log("[desktop] Preparing remote-only thin bundle (no local Python runtime)");
  rmSync(generatedRoot, { recursive: true, force: true });
  mkdirSync(generatedRoot, { recursive: true });
  const placeholder = `${JSON.stringify(
    {
      schema: 0,
      flavor: "thin",
      desktop_version: desktopVersion,
      target: desktopTarget,
    },
    null,
    2,
  )}\n`;
  writeFileSync(join(generatedRoot, "desktop-bundle.json"), placeholder, "utf8");
  writeFileSync(generatedArchive, "", "utf8");
  writeFileSync(join(desktopDir, "generated", "runtime-core.tar.gz"), "", "utf8");
  writeFileSync(
    join(desktopDir, "generated", "runtime-manifest.json"),
    placeholder,
    "utf8",
  );
  console.log("[desktop] Remote-only thin resources ready");
  process.exit(0);
}

const ceBuilder = join(repoRoot, "scripts", "build_ce.py");
const requireClean =
  Boolean(process.env.CI) || process.env.HUGAGENT_RELEASE_BUILD === "1";
if (existsSync(ceBuilder)) {
  const ceArgs = [
    "run",
    "--no-project",
    "--python",
    "3.11",
    "--with-requirements",
    join(desktopDir, "requirements-desktop-build.txt"),
    "python",
    ceBuilder,
    "--out",
    generatedRoot,
  ];
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
  run("uv", ceArgs, {
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

// Desktop dependency inputs remain under desktop/. The private runtime already
// contains their exact result, so the CE source archive does not duplicate them.
console.log(
  `[desktop] Dependency lock ready: ${dependencyFingerprint.slice(0, 12)}`,
);

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
      schema: 2,
      desktop_version: desktopVersion,
      source_revision: revision.stdout.trim(),
      target: desktopTarget,
      dependency_fingerprint: dependencyFingerprint,
    },
    null,
    2,
  )}\n`,
  "utf8",
);

buildDesktopRuntime({
  desktopDir,
  repoRoot,
  sourceRoot: generatedRoot,
  python,
});

console.log("[desktop] Compressing the CE server payload into one resource");
rmSync(generatedArchive, { force: true });
run(
  python.command,
  [
    ...python.prefix,
    join(desktopDir, "scripts", "create-ce-archive.py"),
    "--source",
    generatedRoot,
    "--output",
    generatedArchive,
  ],
  {
    env: {
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
    },
  },
);

console.log(`[desktop] Local server payload ready: ${generatedArchive}`);
