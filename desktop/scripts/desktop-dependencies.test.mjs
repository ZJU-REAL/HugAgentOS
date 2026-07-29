import assert from "node:assert/strict";
import {
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
  for (const excluded of ["oss2", "boto3", "neo4j", "opensandbox"])
    assert.equal(names.has(excluded), false);
});

test("desktop dependency fingerprint is stable and content-addressed", () => {
  const first = desktopDependencyFingerprint(repoRoot);
  const second = desktopDependencyFingerprint(repoRoot);
  assert.match(first, /^[a-f0-9]{64}$/);
  assert.equal(second, first);
  assert.ok(
    readFileSync(join(repoRoot, WINDOWS_DESKTOP_LOCK_FILE), "utf8").length >
      10_000,
  );
});

test("desktop dependency hash is independent of checkout line endings", () => {
  const fixture = mkdtempSync(join(tmpdir(), "desktop-dependencies-"));
  try {
    for (const file of [DESKTOP_REQUIREMENTS_FILE, WINDOWS_DESKTOP_LOCK_FILE]) {
      const contents = readFileSync(join(repoRoot, file), "utf8");
      writeFileSync(join(fixture, file), contents.replaceAll("\n", "\r\n"));
    }
    assert.doesNotThrow(() => readAndValidateWindowsDesktopLock(fixture));
    assert.equal(
      desktopDependencyFingerprint(fixture),
      desktopDependencyFingerprint(repoRoot),
    );
  } finally {
    rmSync(fixture, { recursive: true, force: true });
  }
});

test("Windows bootstrap uses one locked sync and no full pip fallback", () => {
  const installer = readFileSync(
    join(
      desktopDir,
      "resources",
      "server-bootstrap",
      "install-local-server.ps1",
    ),
    "utf8",
  );
  assert.match(installer, /"pip", "sync"/);
  assert.match(
    installer,
    /https:\/\/mirrors\.aliyun\.com\/pypi\/simple/,
  );
  assert.match(installer, /HUGAGENT_PYPI_INDEX_URL/);
  assert.match(installer, /"--default-index"/);
  assert.match(installer, /BundledDependencyFingerprint/);
  assert.match(installer, /Python 依赖未变化，复用现有运行环境/);
  assert.doesNotMatch(installer, /retrying with pip/i);
  assert.doesNotMatch(installer, /SourceDir "requirements\.txt"/);
});
