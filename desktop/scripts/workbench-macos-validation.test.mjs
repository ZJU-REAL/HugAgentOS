import test from "node:test";
import assert from "node:assert/strict";
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { captureSource, excludedSourcePath } from "./workbench-source-snapshot.mjs";
import { validationEnvironment } from "./workbench-macos-validation.mjs";
import { createMatrix } from "./workbench-macos-matrix.mjs";

test("source receipts identify uncommitted source without inventing a commit", async () => {
  const root = realpathSync(fileURLToPath(new URL("../../", import.meta.url)));
  const first = await captureSource(root);
  const second = await captureSource(root);
  assert.match(first.baselineCommit, /^[a-f0-9]{40}$/);
  assert.equal(first.sourceSha256, second.sourceSha256);
  assert.ok(first.files.some(file => file.path === "desktop/scripts/workbench-macos-validation.mjs"));
  assert.ok(first.files.some(file => file.path === "desktop/src-tauri/Cargo.lock"));
  assert.ok(first.files.every(file => !excludedSourcePath(file.path)));
});

test("native source checks need neither credentials nor an ad-hoc signing override", () => {
  const env = validationEnvironment({ PATH: "/existing-tools", TAURI_SIGNING_PRIVATE_KEY: "fixture-secret" }, "/fixture");
  assert.equal(env.TAURI_SIGNING_PRIVATE_KEY, undefined);
  assert.equal(env.HUGAGENT_RELEASE_BUILD, undefined);
  assert.deepEqual(JSON.parse(env.TAURI_CONFIG), { bundle: { resources: [] } });
  assert.throws(() => validationEnvironment({ HUGAGENT_RELEASE_BUILD: "1" }, "/fixture"), /release/);
  assert.throws(() => validationEnvironment({ HUGAGENT_ALLOW_ADHOC: "1" }, "/fixture"), /overrides/);
  assert.throws(() => createMatrix({ kind: "native-source-validation-only", architecture: "arm64" }), /native/);
});

test("source manifests include data-named code but exclude private configs and build/runtime data", () => {
  assert.equal(excludedSourcePath("src/backend/core/data/service.py"), false);
  assert.equal(excludedSourcePath(".env.example"), false);
  for (const path of [".env", ".env.production", "storage/user-file", "data/user-file", "desktop/generated/runtime-core.tar.gz",
    "desktop/src-tauri/target/release/app", "secret.key", "private/server.json"]) {
    assert.equal(excludedSourcePath(path), true);
  }
});
