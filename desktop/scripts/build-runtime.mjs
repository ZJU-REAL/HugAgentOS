import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { basename, join } from "node:path";

import {
  currentDesktopTarget,
  desktopDependencyFingerprint,
  desktopTargetConfig,
  readAndValidateDesktopLock,
} from "./desktop-dependencies.mjs";

export function buildDesktopRuntime({ desktopDir, repoRoot, sourceRoot, python }) {
  const target = currentDesktopTarget();
  const config = desktopTargetConfig(target);
  const dependencyFingerprint = desktopDependencyFingerprint(repoRoot, target);
  const generatedDir = join(desktopDir, "generated");
  const archive = join(generatedDir, "runtime-core.tar.gz");
  const manifestPath = join(generatedDir, "runtime-manifest.json");
  const cached = readJson(manifestPath);
  if (
    cached?.schema === 1 &&
    cached.target === target &&
    cached.dependency_fingerprint === dependencyFingerprint &&
    cached.archive_sha256 === hashFile(archive)
  ) {
    console.log(
      `[desktop] Reusing offline runtime ${target}/${dependencyFingerprint.slice(0, 12)}`,
    );
    return cached;
  }

  assertUv();
  readAndValidateDesktopLock(repoRoot, target);
  const buildRoot = join(generatedDir, `.runtime-build-${target}`);
  const managedRoot = join(buildRoot, "managed");
  const runtimeRoot = join(buildRoot, "runtime");
  const pythonRoot = join(runtimeRoot, "python");
  const executable = join(runtimeRoot, ...config.executable.split("/"));
  const partialLayout = readJson(join(runtimeRoot, "runtime-layout.json"));
  const reusablePartial =
    partialLayout?.schema === 1 &&
    partialLayout.target === target &&
    partialLayout.dependency_fingerprint === dependencyFingerprint &&
    partialLayout.executable === config.executable &&
    existsSync(executable);

  if (reusablePartial) {
    console.log(`[desktop] Resuming validated ${target} runtime staging tree`);
  } else {
    rmSync(buildRoot, { recursive: true, force: true });
    mkdirSync(runtimeRoot, { recursive: true });

    console.log(`[desktop] Installing a private Python 3.11 runtime for ${target}`);
    run("uv", [
      "python",
      "install",
      "3.11",
      "--install-dir",
      managedRoot,
      "--no-bin",
    ], { cwd: repoRoot });
    const distributions = readdirSync(managedRoot, { withFileTypes: true }).filter(
      (entry) => entry.isDirectory() && entry.name.startsWith("cpython-3.11"),
    );
    if (distributions.length !== 1) {
      throw new Error(`Expected one managed CPython distribution, found ${distributions.length}`);
    }
    renameSync(join(managedRoot, distributions[0].name), pythonRoot);
    if (!existsSync(executable)) {
      throw new Error(`Managed Python executable is missing: ${executable}`);
    }

    console.log(`[desktop] Installing the locked ${target} dependency set`);
    run(
      "uv",
      [
        "pip",
        "sync",
        "--python",
        executable,
        "--break-system-packages",
        "--only-binary",
        ":all:",
        join(repoRoot, config.lockFile),
      ],
      { cwd: repoRoot },
    );
  }
  run("uv", ["pip", "check", "--python", executable], { cwd: repoRoot });

  const smokeTest = join(runtimeRoot, "runtime-smoke.py");
  copyFileSync(join(desktopDir, "scripts", "runtime-smoke.py"), smokeTest);
  const pythonVersion = capture(executable, ["-c", "import platform; print(platform.python_version())"]);
  const layout = {
    schema: 1,
    target,
    python_version: pythonVersion,
    dependency_fingerprint: dependencyFingerprint,
    executable: config.executable,
    smoke_test: "runtime-smoke.py",
  };
  writeFileSync(
    join(runtimeRoot, "runtime-layout.json"),
    `${JSON.stringify(layout, null, 2)}\n`,
    "utf8",
  );
  run(executable, [smokeTest, "--source", sourceRoot], { cwd: sourceRoot });
  signMacRuntime(runtimeRoot);

  rmSync(archive, { force: true });
  run(
    python.command,
    [
      ...python.prefix,
      join(desktopDir, "scripts", "create-runtime-archive.py"),
      "--source",
      runtimeRoot,
      "--output",
      archive,
    ],
    { cwd: repoRoot },
  );
  const manifest = {
    ...layout,
    archive: basename(archive),
    archive_sha256: hashFile(archive),
    archive_size: statSync(archive).size,
    unpacked_size: directorySize(runtimeRoot),
  };
  writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
  rmSync(buildRoot, { recursive: true, force: true });
  console.log(
    `[desktop] Offline runtime ready: ${target}, ${(manifest.archive_size / 1048576).toFixed(1)} MiB`,
  );
  return manifest;
}

function run(command, args, options = {}) {
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

function capture(command, args) {
  const result = spawnSync(command, args, { encoding: "utf8", shell: false });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(result.stderr || `${command} failed`);
  return result.stdout.trim();
}

function assertUv() {
  const result = spawnSync("uv", ["--version"], { stdio: "ignore", shell: false });
  if (result.status !== 0) {
    throw new Error(
      "uv is required on the release builder; install the version pinned in requirements-desktop.txt.",
    );
  }
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function hashFile(path) {
  if (!existsSync(path)) return null;
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function directorySize(root) {
  let total = 0;
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) visit(path);
      else if (!entry.isSymbolicLink()) total += lstatSync(path).size;
    }
  };
  visit(root);
  return total;
}

function signMacRuntime(root) {
  if (process.platform !== "darwin") return;
  const identity = process.env.APPLE_SIGNING_IDENTITY?.trim() || "-";
  if (identity === "-") {
    console.warn(
      "[desktop] Apple signing identity unavailable; using ad-hoc signing for the macOS runtime.",
    );
  }
  const files = [];
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) visit(path);
      else if (!entry.isSymbolicLink()) files.push(path);
    }
  };
  visit(root);
  for (const path of files) {
    const kind = capture("/usr/bin/file", ["-b", path]);
    if (!kind.includes("Mach-O")) continue;
    const args = ["--force", "--sign", identity];
    if (identity !== "-") args.push("--timestamp", "--options", "runtime");
    args.push(path);
    run("/usr/bin/codesign", args);
  }
}
