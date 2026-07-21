import { readFileSync } from "node:fs";
import { join } from "node:path";

export function readDesktopVersion(desktopDir) {
  const packageJson = JSON.parse(
    readFileSync(join(desktopDir, "package.json"), "utf8"),
  );
  const tauriConfig = JSON.parse(
    readFileSync(join(desktopDir, "src-tauri", "tauri.conf.json"), "utf8"),
  );
  const cargoToml = readFileSync(
    join(desktopDir, "src-tauri", "Cargo.toml"),
    "utf8",
  );
  const cargoVersion = cargoToml.match(/^version\s*=\s*"([^"]+)"/m)?.[1];

  if (
    !cargoVersion ||
    packageJson.version !== tauriConfig.version ||
    packageJson.version !== cargoVersion
  ) {
    throw new Error(
      `Desktop version mismatch: package=${packageJson.version}, tauri=${tauriConfig.version}, cargo=${cargoVersion || "missing"}`,
    );
  }

  return packageJson.version;
}

export function validateDesktopReleaseTag(desktopDir, releaseTag) {
  const version = readDesktopVersion(desktopDir);
  const expectedTag = `desktop-v${version}`;
  if (releaseTag !== expectedTag) {
    throw new Error(
      `Desktop release tag mismatch: received=${releaseTag || "missing"}, expected=${expectedTag}`,
    );
  }
  return { version, expectedTag };
}
