import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

export const DESKTOP_REQUIREMENTS_FILE = "desktop/requirements-desktop.txt";
export const MACOS_DESKTOP_OVERRIDES_FILE =
  "desktop/requirements-desktop-macos-overrides.txt";
export const DESKTOP_LOCK_INPUT_MARKER = "# input-sha256: ";

export const DESKTOP_TARGETS = Object.freeze({
  "windows-x86_64": Object.freeze({
    lockFile: "desktop/requirements-desktop-windows-py311.lock",
    pythonPlatform: "x86_64-pc-windows-msvc",
    label: "CPython 3.11 on Windows x86_64",
    executable: "python/python.exe",
  }),
  "darwin-aarch64": Object.freeze({
    lockFile: "desktop/requirements-desktop-macos-aarch64-py311.lock",
    pythonPlatform: "aarch64-apple-darwin",
    label: "CPython 3.11 on macOS Apple Silicon",
    executable: "python/bin/python3.11",
    overridesFile: MACOS_DESKTOP_OVERRIDES_FILE,
  }),
  "darwin-x86_64": Object.freeze({
    lockFile: "desktop/requirements-desktop-macos-x86_64-py311.lock",
    pythonPlatform: "x86_64-apple-darwin",
    label: "CPython 3.11 on macOS Intel",
    executable: "python/bin/python3.11",
    overridesFile: MACOS_DESKTOP_OVERRIDES_FILE,
  }),
  "linux-x86_64": Object.freeze({
    lockFile: "desktop/requirements-desktop-linux-x86_64-py311.lock",
    pythonPlatform: "x86_64-unknown-linux-gnu",
    label: "CPython 3.11 on Linux x86_64",
    executable: "python/bin/python3.11",
  }),
  "linux-aarch64": Object.freeze({
    lockFile: "desktop/requirements-desktop-linux-aarch64-py311.lock",
    // onnxruntime (via markitdown/magika) first publishes aarch64 wheels at
    // manylinux_2_28. UOS 1070 provides glibc 2.28, so this is both the oldest
    // resolvable dependency baseline and the exact supported system floor.
    pythonPlatform: "aarch64-manylinux_2_28",
    label: "CPython 3.11 on Linux aarch64 (UOS 1070 / manylinux 2.28)",
    executable: "python/bin/python3.11",
  }),
});

export const WINDOWS_DESKTOP_LOCK_FILE =
  DESKTOP_TARGETS["windows-x86_64"].lockFile;
export const WINDOWS_LOCK_INPUT_MARKER = DESKTOP_LOCK_INPUT_MARKER;

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function readNormalizedText(path) {
  return readFileSync(path, "utf8").replaceAll("\r\n", "\n");
}

export function currentDesktopTarget(platform = process.platform, arch = process.arch) {
  const target =
    platform === "win32" && arch === "x64"
      ? "windows-x86_64"
      : platform === "darwin" && arch === "arm64"
        ? "darwin-aarch64"
        : platform === "darwin" && arch === "x64"
          ? "darwin-x86_64"
          : platform === "linux" && arch === "x64"
            ? "linux-x86_64"
            : platform === "linux" && arch === "arm64"
              ? "linux-aarch64"
            : null;
  if (!target) {
    throw new Error(`Unsupported desktop release target: ${platform}/${arch}`);
  }
  return target;
}

export function desktopTargetConfig(target) {
  const config = DESKTOP_TARGETS[target];
  if (!config) throw new Error(`Unknown desktop target: ${target}`);
  return config;
}

function targetInputFiles(target) {
  const config = desktopTargetConfig(target);
  return [DESKTOP_REQUIREMENTS_FILE, config.overridesFile].filter(Boolean);
}

export function desktopTargetInputHash(root, target) {
  const hash = createHash("sha256");
  hash.update("desktop-lock-input-v2\0");
  for (const file of targetInputFiles(target)) {
    hash.update(file);
    hash.update("\0");
    hash.update(readNormalizedText(join(root, file)));
    hash.update("\0");
  }
  return hash.digest("hex");
}

export function desktopRequirementsInputHash(root) {
  return sha256(readNormalizedText(join(root, DESKTOP_REQUIREMENTS_FILE)));
}

export function readAndValidateDesktopLock(root, target) {
  const config = desktopTargetConfig(target);
  const lock = readNormalizedText(join(root, config.lockFile));
  const marker = lock
    .split(/\r?\n/, 8)
    .find((line) => line.startsWith(DESKTOP_LOCK_INPUT_MARKER));
  const expected = desktopTargetInputHash(root, target);
  const actual = marker?.slice(DESKTOP_LOCK_INPUT_MARKER.length).trim();
  if (actual !== expected) {
    throw new Error(
      `${config.lockFile} is stale; run ` +
        "`npm --prefix desktop run lock:desktop`.",
    );
  }
  return lock;
}

export function readAndValidateWindowsDesktopLock(root) {
  return readAndValidateDesktopLock(root, "windows-x86_64");
}

export function desktopDependencyFingerprint(
  root,
  target = currentDesktopTarget(),
) {
  const config = desktopTargetConfig(target);
  const lock = readAndValidateDesktopLock(root, target);
  const hash = createHash("sha256");
  hash.update("desktop-dependencies-v2\0");
  hash.update(target);
  hash.update("\0");
  for (const file of targetInputFiles(target)) {
    hash.update(file);
    hash.update("\0");
    hash.update(readNormalizedText(join(root, file)));
    hash.update("\0");
  }
  hash.update(config.lockFile);
  hash.update("\0");
  hash.update(lock);
  return hash.digest("hex");
}
