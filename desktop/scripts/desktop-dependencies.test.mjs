import assert from "node:assert/strict";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  DESKTOP_TARGETS,
  DESKTOP_RUNTIME_INPUT_FILES,
  DESKTOP_REQUIREMENTS_FILE,
  WINDOWS_DESKTOP_LOCK_FILE,
  desktopDependencyFingerprint,
  readAndValidateWindowsDesktopLock,
} from "./desktop-dependencies.mjs";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(desktopDir, "..");

function requirementNames(contents) {
  return new Set(
    contents
      .split(/\r?\n/)
      .filter((line) => line && !line.startsWith(" ") && !line.startsWith("#"))
      .map((line) => line.match(/^([A-Za-z0-9_.-]+)/)?.[1])
      .filter(Boolean)
      .map((name) => name.toLowerCase().replaceAll("_", "-")),
  );
}

test("desktop dependency profile excludes server and container providers", () => {
  const requirements = readFileSync(
    join(repoRoot, DESKTOP_REQUIREMENTS_FILE),
    "utf8",
  );
  const names = requirementNames(requirements);
  for (const excluded of [
    "aliyun-python-sdk-core",
    "bandit",
    "boto3",
    "botocore",
    "e2b-code-interpreter",
    "neo4j",
    "opensandbox",
    "opensandbox-code-interpreter",
    "oss2",
    "psycopg2-binary",
    "safety",
  ]) {
    assert.equal(names.has(excluded), false, `${excluded} must stay excluded`);
  }
  for (const required of [
    "agentscope",
    "fastapi",
    "mem0ai",
    "milvus-lite",
    "pymilvus",
    "python-docx",
    "scipy",
    "uv",
  ]) {
    assert.equal(names.has(required), true, `${required} must stay available`);
  }
});

test("Windows Python 3.11 lock is exact and matches its desktop input", () => {
  const lock = readAndValidateWindowsDesktopLock(repoRoot);
  const names = requirementNames(lock);
  assert.ok(names.size > 100, "the transitive desktop lock must be complete");
  for (const line of lock.split(/\r?\n/)) {
    if (/^[A-Za-z0-9_.-]+/.test(line)) {
      assert.match(line, /^[A-Za-z0-9_.-]+==[^\s]+$/);
    }
  }
  const mcpVersion = lock.match(/^mcp==([^\s]+)$/m)?.[1];
  assert.ok(mcpVersion, "the lock must contain mcp");
  assert.equal(Number(mcpVersion.split(".")[0]), 1);
  assert.match(lock, /^pymilvus==2\.5\.18$/m);
  assert.match(lock, /^milvus-lite==3\.1\.0$/m);
  // Some platform locks pull boto3 through LiteLLM; it is not a storage backend.
  if (names.has("boto3"))
    assert.match(lock, /^boto3==[^\s]+\r?\n    # via litellm$/m);
  for (const excluded of ["oss2", "neo4j", "opensandbox"])
    assert.equal(names.has(excluded), false);
});

test("desktop dependency fingerprint is stable and content-addressed", () => {
  for (const target of Object.keys(DESKTOP_TARGETS)) {
    const first = desktopDependencyFingerprint(repoRoot, target);
    const second = desktopDependencyFingerprint(repoRoot, target);
    assert.match(first, /^[a-f0-9]{64}$/);
    assert.equal(second, first);
  }
  assert.ok(
    readFileSync(join(repoRoot, WINDOWS_DESKTOP_LOCK_FILE), "utf8").length >
      10_000,
  );
});

test("desktop dependency hash is independent of checkout line endings", () => {
  const fixture = mkdtempSync(join(tmpdir(), "desktop-dependencies-"));
  try {
    mkdirSync(join(fixture, "desktop"), { recursive: true });
    for (const file of [DESKTOP_REQUIREMENTS_FILE, WINDOWS_DESKTOP_LOCK_FILE, ...DESKTOP_RUNTIME_INPUT_FILES]) {
      mkdirSync(dirname(join(fixture, file)), { recursive: true });
      const contents = readFileSync(join(repoRoot, file), "utf8");
      writeFileSync(join(fixture, file), contents.replaceAll("\n", "\r\n"));
    }
    assert.doesNotThrow(() => readAndValidateWindowsDesktopLock(fixture));
    assert.equal(
      desktopDependencyFingerprint(fixture, "windows-x86_64"),
      desktopDependencyFingerprint(repoRoot, "windows-x86_64"),
    );
  } finally {
    rmSync(fixture, { recursive: true, force: true });
  }
});

test("all supported desktop targets have exact Python 3.11 locks", () => {
  for (const [target, config] of Object.entries(DESKTOP_TARGETS)) {
    const lock = readFileSync(join(repoRoot, config.lockFile), "utf8");
    assert.match(lock, /# input-sha256: [a-f0-9]{64}/);
    assert.ok(requirementNames(lock).size > 100, `${target} lock is incomplete`);
    for (const line of lock.split(/\r?\n/)) {
      if (/^[A-Za-z0-9_.-]+/.test(line)) {
        assert.match(line, /^[A-Za-z0-9_.-]+==[^\s]+$/);
      }
    }
  }
});


test("changing native tools invalidates a cached desktop runtime", () => {
  const fixture = mkdtempSync(join(tmpdir(), "desktop-native-fingerprint-"));
  try {
    mkdirSync(join(fixture, "desktop"), { recursive: true });
    for (const file of [DESKTOP_REQUIREMENTS_FILE, WINDOWS_DESKTOP_LOCK_FILE, ...DESKTOP_RUNTIME_INPUT_FILES]) {
      mkdirSync(dirname(join(fixture, file)), { recursive: true });
      writeFileSync(join(fixture, file), readFileSync(join(repoRoot, file)));
    }
    const tools = join(fixture, "desktop/native-tools.json");
    writeFileSync(tools, JSON.stringify({officecli:{version:"1.0.143"}}));
    const before = desktopDependencyFingerprint(fixture, "windows-x86_64");
    writeFileSync(tools, JSON.stringify({officecli:{version:"1.0.144"}}));
    assert.notEqual(desktopDependencyFingerprint(fixture, "windows-x86_64"), before);
  } finally { rmSync(fixture, { recursive: true, force: true }); }
});


test("changing the runtime smoke check invalidates the cached bundle", () => {
  const fixture = mkdtempSync(join(tmpdir(), "desktop-recipe-fingerprint-"));
  try {
    for (const file of [DESKTOP_REQUIREMENTS_FILE, WINDOWS_DESKTOP_LOCK_FILE, ...DESKTOP_RUNTIME_INPUT_FILES]) {
      mkdirSync(dirname(join(fixture, file)), { recursive: true });
      writeFileSync(join(fixture, file), readFileSync(join(repoRoot, file)));
    }
    const before = desktopDependencyFingerprint(fixture, "windows-x86_64");
    const smoke = join(fixture, "desktop/scripts/runtime-smoke.py");
    writeFileSync(smoke, readFileSync(smoke, "utf8") + "\n# updated verification recipe\n");
    assert.notEqual(desktopDependencyFingerprint(fixture, "windows-x86_64"), before);
  } finally { rmSync(fixture, { recursive: true, force: true }); }
});

test("changing Mac signing identity replaces the cached and installed runtime", () => {
  const old = { APPLE_SIGNING_IDENTITY: "-", HUGAGENT_ALLOW_ADHOC: "1" };
  const next = {
    APPLE_SIGNING_IDENTITY: "Developer ID Application: Example Company (ABCDE12345)",
    APPLE_TEAM_ID: "ABCDE12345",
  };
  for (const target of ["darwin-aarch64", "darwin-x86_64"]) {
    assert.notEqual(desktopDependencyFingerprint(repoRoot, target, old),
      desktopDependencyFingerprint(repoRoot, target, next));
    assert.equal(desktopDependencyFingerprint(repoRoot, target, next),
      desktopDependencyFingerprint(repoRoot, target, { ...next, APPLE_PASSWORD: "rotated-password" }));
  }
  assert.equal(desktopDependencyFingerprint(repoRoot, "windows-x86_64", old),
    desktopDependencyFingerprint(repoRoot, "windows-x86_64", next));
});

test("Mac runtime fingerprint separates signing modes and normalizes certificate fingerprints", () => {
  const env = {
    HUGAGENT_MACOS_SIGNING_MODE: "self-signed",
    APPLE_SIGNING_IDENTITY: "abcdef0123456789abcdef0123456789abcdef01",
  };
  for (const target of ["darwin-aarch64", "darwin-x86_64"]) {
    const fingerprint = desktopDependencyFingerprint(repoRoot, target, env);
    assert.equal(fingerprint, desktopDependencyFingerprint(repoRoot, target, {
      ...env, APPLE_SIGNING_IDENTITY: env.APPLE_SIGNING_IDENTITY.toUpperCase(),
    }));
    assert.notEqual(fingerprint, desktopDependencyFingerprint(repoRoot, target, {
      ...env, HUGAGENT_MACOS_SIGNING_MODE: "developer-id",
    }));
    assert.notEqual(fingerprint, desktopDependencyFingerprint(repoRoot, target, {
      ...env, APPLE_SIGNING_IDENTITY: "1".repeat(40),
    }));
    assert.equal(fingerprint, desktopDependencyFingerprint(repoRoot, target, {
      ...env, APPLE_TEAM_ID: "IGNORED123", APPLE_PASSWORD: "irrelevant-fixture",
    }));
  }
});
