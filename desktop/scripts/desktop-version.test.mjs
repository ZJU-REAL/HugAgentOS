import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  readDesktopVersion,
  resolveDesktopReleaseTag,
  setDesktopVersion,
  validateDesktopReleaseTag,
} from "./desktop-version.mjs";

function createDesktopFixture(versions = {}) {
  const root = mkdtempSync(join(tmpdir(), "hugagent-desktop-version-"));
  const desktopDir = join(root, "desktop");
  mkdirSync(join(desktopDir, "src-tauri"), { recursive: true });
  writeFileSync(
    join(desktopDir, "package.json"),
    JSON.stringify({ version: versions.package || "1.2.3" }),
  );
  writeFileSync(
    join(desktopDir, "package-lock.json"),
    JSON.stringify({
      version: versions.packageLock || "1.2.3",
      packages: { "": { version: versions.packageLockRoot || "1.2.3" } },
    }),
  );
  writeFileSync(
    join(desktopDir, "src-tauri", "tauri.conf.json"),
    JSON.stringify({ version: versions.tauri || "1.2.3" }),
  );
  writeFileSync(
    join(desktopDir, "src-tauri", "Cargo.toml"),
    `[package]\nversion = "${versions.cargo || "1.2.3"}"\n`,
  );
  writeFileSync(
    join(desktopDir, "src-tauri", "Cargo.lock"),
    `[[package]]\nname = "hugagent-desktop"\nversion = "${versions.cargoLock || "1.2.3"}"\n`,
  );
  return { root, desktopDir };
}

test("accepts matching desktop versions and release tag", () => {
  const fixture = createDesktopFixture();
  try {
    assert.equal(readDesktopVersion(fixture.desktopDir), "1.2.3");
    assert.deepEqual(
      validateDesktopReleaseTag(fixture.desktopDir, "desktop-v1.2.3"),
      {
        version: "1.2.3",
        expectedTag: "desktop-v1.2.3",
      },
    );
  } finally {
    rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("rejects mismatched desktop version files", () => {
  const fixture = createDesktopFixture({ cargo: "1.2.4" });
  try {
    assert.throws(
      () => readDesktopVersion(fixture.desktopDir),
      /Desktop version mismatch/,
    );
  } finally {
    rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("rejects a release tag that does not match the desktop version", () => {
  const fixture = createDesktopFixture();
  try {
    assert.throws(
      () => validateDesktopReleaseTag(fixture.desktopDir, "desktop-v1.2.2"),
      /received=desktop-v1\.2\.2, expected=desktop-v1\.2\.3/,
    );
  } finally {
    rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("manual release derives its tag from the committed desktop version", () => {
  const fixture = createDesktopFixture();
  try {
    assert.deepEqual(resolveDesktopReleaseTag(fixture.desktopDir, ""), {
      version: "1.2.3",
      expectedTag: "desktop-v1.2.3",
    });
  } finally {
    rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("version command synchronizes every desktop manifest", () => {
  const fixture = createDesktopFixture();
  try {
    assert.equal(setDesktopVersion(fixture.desktopDir, "1.3.0"), "1.3.0");
    assert.equal(readDesktopVersion(fixture.desktopDir), "1.3.0");
    assert.deepEqual(
      validateDesktopReleaseTag(fixture.desktopDir, "desktop-v1.3.0"),
      { version: "1.3.0", expectedTag: "desktop-v1.3.0" },
    );
  } finally {
    rmSync(fixture.root, { recursive: true, force: true });
  }
});
