import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { assertPinnedUv, pinnedUvVersion } from "./desktop-dependencies.mjs";
import { fetchPublishedRuntime, publishedRuntimeNames } from "./published-runtime.mjs";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const target = "windows-x86_64";
const fingerprint = "a".repeat(64);

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), "published-runtime-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const release = join(root, "release");
  const generated = join(root, "generated");
  mkdirSync(release);
  mkdirSync(generated);
  return {
    release,
    archivePath: join(generated, "runtime-core.tar.gz"),
    manifestPath: join(generated, "runtime-manifest.json"),
  };
}

function publish(release, archiveBytes, manifestOverrides = {}) {
  const names = publishedRuntimeNames(target, fingerprint);
  writeFileSync(join(release, names.archive), archiveBytes);
  writeFileSync(join(release, names.manifest), JSON.stringify({
    schema: 1,
    target,
    dependency_fingerprint: fingerprint,
    archive: "runtime-core.tar.gz",
    archive_sha256: createHash("sha256").update(archiveBytes).digest("hex"),
    ...manifestOverrides,
  }));
}

test("published runtime names are content addressed and reject unsafe identities", () => {
  assert.deepEqual(publishedRuntimeNames(target, fingerprint), {
    archive: `runtime-${target}-${fingerprint}.tar.gz`,
    manifest: `runtime-${target}-${fingerprint}.json`,
  });
  assert.throws(() => publishedRuntimeNames("../x", fingerprint));
  assert.throws(() => publishedRuntimeNames(target, "abc"));
});

test("a published runtime becomes the build cache with identical bytes", (t) => {
  const paths = fixture(t);
  publish(paths.release, "runtime-bytes");
  assert.equal(fetchPublishedRuntime({ source: paths.release, target, fingerprint, ...paths }), true);
  assert.equal(readFileSync(paths.archivePath, "utf8"), "runtime-bytes");
  assert.equal(JSON.parse(readFileSync(paths.manifestPath, "utf8")).dependency_fingerprint, fingerprint);
});

test("an unpublished fingerprint or an unset source falls back to building", (t) => {
  const paths = fixture(t);
  assert.equal(fetchPublishedRuntime({ source: paths.release, target, fingerprint, ...paths }), false);
  assert.equal(fetchPublishedRuntime({ source: "", target, fingerprint, ...paths }), false);
});

test("a corrupt or mismatched published runtime fails without touching the cache", (t) => {
  const paths = fixture(t);
  publish(paths.release, "runtime-bytes", { archive_sha256: "0".repeat(64) });
  assert.throws(() => fetchPublishedRuntime({ source: paths.release, target, fingerprint, ...paths }),
    /does not match its manifest/);
  publish(paths.release, "runtime-bytes", { dependency_fingerprint: "b".repeat(64) });
  assert.throws(() => fetchPublishedRuntime({ source: paths.release, target, fingerprint, ...paths }),
    /describes another runtime/);
  assert.equal(existsSync(paths.archivePath), false);
  assert.equal(existsSync(paths.manifestPath), false);
});

test("the release builder must use the uv version pinned in the runtime inputs", () => {
  const pinned = pinnedUvVersion(repoRoot);
  const runner = (stdout) => () => ({ status: 0, stdout });
  assertPinnedUv(repoRoot, runner(`uv ${pinned} (abc 2026-01-01)\n`));
  assert.throws(() => assertPinnedUv(repoRoot, runner("uv 0.0.1\n")), /required on the release builder/);
  assert.throws(() => assertPinnedUv(repoRoot, () => ({ status: 1, stdout: "" })), /found none/);
});
