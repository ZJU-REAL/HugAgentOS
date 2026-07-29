import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

export const DESKTOP_REQUIREMENTS_FILE = "requirements-desktop.txt";
export const WINDOWS_DESKTOP_LOCK_FILE =
  "requirements-desktop-windows-py311.lock";
export const WINDOWS_LOCK_INPUT_MARKER = "# input-sha256: ";

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function readNormalizedText(path) {
  return readFileSync(path, "utf8").replaceAll("\r\n", "\n");
}

export function desktopRequirementsInputHash(root) {
  return sha256(readNormalizedText(join(root, DESKTOP_REQUIREMENTS_FILE)));
}

export function readAndValidateWindowsDesktopLock(root) {
  const lock = readNormalizedText(join(root, WINDOWS_DESKTOP_LOCK_FILE));
  const marker = lock
    .split(/\r?\n/, 8)
    .find((line) => line.startsWith(WINDOWS_LOCK_INPUT_MARKER));
  const expected = desktopRequirementsInputHash(root);
  const actual = marker?.slice(WINDOWS_LOCK_INPUT_MARKER.length).trim();
  if (actual !== expected) {
    throw new Error(
      `${WINDOWS_DESKTOP_LOCK_FILE} is stale; run ` +
        "`npm --prefix desktop run lock:windows`.",
    );
  }
  return lock;
}

export function desktopDependencyFingerprint(root) {
  const requirements = readNormalizedText(join(root, DESKTOP_REQUIREMENTS_FILE));
  const windowsLock = readAndValidateWindowsDesktopLock(root);
  const hash = createHash("sha256");
  hash.update("desktop-dependencies-v1\0");
  hash.update(DESKTOP_REQUIREMENTS_FILE);
  hash.update("\0");
  hash.update(requirements);
  hash.update("\0");
  hash.update(WINDOWS_DESKTOP_LOCK_FILE);
  hash.update("\0");
  hash.update(windowsLock);
  return hash.digest("hex");
}
