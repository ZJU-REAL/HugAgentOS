import { spawnSync } from "node:child_process";
import {
  chmodSync,
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readlinkSync,
  rmSync,
  symlinkSync,
} from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";

export const CE_EDITION_MARKER = ".hugagent-edition";

function git(repoRoot, args, options = {}) {
  const result = spawnSync("git", args, {
    cwd: repoRoot,
    encoding: "utf8",
    shell: false,
    ...options,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(
      `git ${args.join(" ")} failed with exit code ${result.status}`,
    );
  }
  return result.stdout;
}

export function assertDerivedCeRepository(repoRoot) {
  const markerPath = resolve(repoRoot, CE_EDITION_MARKER);
  const edition = existsSync(markerPath)
    ? readFileSync(markerPath, "utf8").trim()
    : "";
  if (edition !== "ce") {
    throw new Error(
      `CE generator is unavailable and ${CE_EDITION_MARKER} does not identify this checkout as a derived CE repository`,
    );
  }
}

export function assertCleanRepository(repoRoot) {
  // Tauri may touch Cargo.toml while inspecting dependencies. On Windows with
  // CRLF conversion, `git status` can report a stat-only rewrite as modified
  // even when the normalized content is identical to HEAD. Compare tracked
  // content directly, then audit non-ignored untracked paths separately.
  const tracked = spawnSync("git", ["diff", "--quiet", "HEAD", "--"], {
    cwd: repoRoot,
    encoding: "utf8",
    shell: false,
  });
  if (tracked.error) throw tracked.error;
  if (tracked.status === 1) {
    throw new Error(
      "Desktop release payloads must be built from a clean Git checkout",
    );
  }
  if (tracked.status !== 0) {
    throw new Error(
      `git diff --quiet HEAD -- failed with exit code ${tracked.status}`,
    );
  }

  const untracked = spawnSync(
    "git",
    ["ls-files", "--others", "--exclude-standard", "-z"],
    { cwd: repoRoot, encoding: "utf8", shell: false },
  );
  if (untracked.error) throw untracked.error;
  if (untracked.status !== 0) {
    throw new Error(
      `git ls-files --others --exclude-standard failed with exit code ${untracked.status}`,
    );
  }
  if (untracked.stdout.length > 0) {
    throw new Error(
      "Desktop release payloads must be built from a clean Git checkout",
    );
  }
}

function assertSafeTrackedPath(repoRoot, relativePath) {
  if (!relativePath || isAbsolute(relativePath)) {
    throw new Error(`Unsafe tracked path: ${relativePath || "<empty>"}`);
  }
  const sourcePath = resolve(repoRoot, relativePath);
  const fromRoot = relative(repoRoot, sourcePath);
  if (
    fromRoot === ".." ||
    fromRoot.startsWith(`..${sep}`) ||
    isAbsolute(fromRoot)
  ) {
    throw new Error(`Tracked path escapes the repository: ${relativePath}`);
  }
  return sourcePath;
}

export function stageTrackedCeRepository(repoRoot, outputRoot, options = {}) {
  assertDerivedCeRepository(repoRoot);
  if (options.requireClean) assertCleanRepository(repoRoot);

  const resolvedOutput = resolve(outputRoot);
  const outputFromRoot = relative(repoRoot, resolvedOutput);
  if (
    outputFromRoot === "" ||
    outputFromRoot === ".." ||
    outputFromRoot.startsWith(`..${sep}`) ||
    isAbsolute(outputFromRoot)
  ) {
    throw new Error(
      "The CE payload output must be a dedicated directory inside the repository",
    );
  }

  const tracked = git(repoRoot, ["ls-files", "-z"]).split("\0").filter(Boolean);

  rmSync(resolvedOutput, {
    recursive: true,
    force: true,
    maxRetries: 3,
    retryDelay: 200,
  });

  let copied = 0;
  for (const relativePath of tracked) {
    const sourcePath = assertSafeTrackedPath(repoRoot, relativePath);
    if (!existsSync(sourcePath)) continue;

    const destinationPath = resolve(resolvedOutput, relativePath);
    const fromOutput = relative(resolvedOutput, destinationPath);
    if (
      fromOutput === ".." ||
      fromOutput.startsWith(`..${sep}`) ||
      isAbsolute(fromOutput)
    ) {
      throw new Error(`Tracked path escapes the CE payload: ${relativePath}`);
    }

    const sourceStat = lstatSync(sourcePath);
    mkdirSync(dirname(destinationPath), { recursive: true });
    if (sourceStat.isSymbolicLink()) {
      symlinkSync(readlinkSync(sourcePath), destinationPath);
    } else if (sourceStat.isFile()) {
      copyFileSync(sourcePath, destinationPath);
      if (process.platform !== "win32")
        chmodSync(destinationPath, sourceStat.mode);
    } else {
      throw new Error(`Unsupported tracked filesystem entry: ${relativePath}`);
    }
    copied += 1;
  }

  return copied;
}
