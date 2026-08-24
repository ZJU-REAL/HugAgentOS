import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const SEMVER = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/;

function readJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function writeJson(path, value) {
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

export function readDesktopVersions(desktopDir) {
  const packageJson = readJson(join(desktopDir, "package.json"));
  const packageLock = readJson(join(desktopDir, "package-lock.json"));
  const tauriConfig = readJson(
    join(desktopDir, "src-tauri", "tauri.conf.json"),
  );
  const cargoToml = readFileSync(
    join(desktopDir, "src-tauri", "Cargo.toml"),
    "utf8",
  );
  const cargoLock = readFileSync(
    join(desktopDir, "src-tauri", "Cargo.lock"),
    "utf8",
  );
  const cargoVersion = cargoToml.match(/^version\s*=\s*"([^"]+)"/m)?.[1];
  const cargoLockVersion = cargoLock.match(
    /\[\[package\]\]\r?\nname = "hugagent-desktop"\r?\nversion = "([^"]+)"/,
  )?.[1];

  const versions = {
    package: packageJson.version,
    packageLock: packageLock.version,
    packageLockRoot: packageLock.packages?.[""]?.version,
    tauri: tauriConfig.version,
    cargo: cargoVersion,
    cargoLock: cargoLockVersion,
  };
  const uosDir = join(desktopDir, "..", "desktop-uos");
  if (existsSync(join(uosDir, "package.json"))) {
    const uosPackage = readJson(join(uosDir, "package.json"));
    const uosLock = readJson(join(uosDir, "package-lock.json"));
    versions.uosPackage = uosPackage.version;
    versions.uosPackageLock = uosLock.version;
    versions.uosPackageLockRoot = uosLock.packages?.[""]?.version;
  }
  return versions;
}

export function readDesktopVersion(desktopDir) {
  const versions = readDesktopVersions(desktopDir);
  const version = versions.package;

  if (
    !version ||
    !SEMVER.test(version) ||
    Object.values(versions).some((candidate) => candidate !== version)
  ) {
    throw new Error(
      `Desktop version mismatch: ${Object.entries(versions)
        .map(([name, value]) => `${name}=${value || "missing"}`)
        .join(", ")}. Run: npm --prefix desktop run version:desktop -- <X.Y.Z>`,
    );
  }

  return version;
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

export function resolveDesktopReleaseTag(desktopDir, releaseTag) {
  const version = readDesktopVersion(desktopDir);
  const expectedTag = `desktop-v${version}`;
  if (releaseTag) {
    return validateDesktopReleaseTag(desktopDir, releaseTag);
  }
  return { version, expectedTag };
}

export function setDesktopVersion(desktopDir, version) {
  if (!SEMVER.test(version || "")) {
    throw new Error(`Invalid desktop version: ${version || "missing"}`);
  }

  const packagePath = join(desktopDir, "package.json");
  const packageLockPath = join(desktopDir, "package-lock.json");
  const tauriPath = join(desktopDir, "src-tauri", "tauri.conf.json");
  const cargoPath = join(desktopDir, "src-tauri", "Cargo.toml");
  const cargoLockPath = join(desktopDir, "src-tauri", "Cargo.lock");
  const uosDir = join(desktopDir, "..", "desktop-uos");

  const packageJson = readJson(packagePath);
  packageJson.version = version;
  writeJson(packagePath, packageJson);

  const packageLock = readJson(packageLockPath);
  packageLock.version = version;
  if (packageLock.packages?.[""]) {
    packageLock.packages[""].version = version;
  }
  writeJson(packageLockPath, packageLock);

  const tauriConfig = readJson(tauriPath);
  tauriConfig.version = version;
  writeJson(tauriPath, tauriConfig);

  const cargoToml = readFileSync(cargoPath, "utf8").replace(
    /(^\[package\][\s\S]*?^version\s*=\s*")[^"]+(".*$)/m,
    `$1${version}$2`,
  );
  writeFileSync(cargoPath, cargoToml, "utf8");

  const cargoLock = readFileSync(cargoLockPath, "utf8").replace(
    /(\[\[package\]\]\r?\nname = "hugagent-desktop"\r?\nversion = ")[^"]+("\r?\n)/,
    `$1${version}$2`,
  );
  writeFileSync(cargoLockPath, cargoLock, "utf8");

  if (existsSync(join(uosDir, "package.json"))) {
    const uosPackagePath = join(uosDir, "package.json");
    const uosLockPath = join(uosDir, "package-lock.json");
    const uosPackage = readJson(uosPackagePath);
    uosPackage.version = version;
    writeJson(uosPackagePath, uosPackage);
    const uosLock = readJson(uosLockPath);
    uosLock.version = version;
    if (uosLock.packages?.[""]) uosLock.packages[""].version = version;
    writeJson(uosLockPath, uosLock);
  }

  return readDesktopVersion(desktopDir);
}
