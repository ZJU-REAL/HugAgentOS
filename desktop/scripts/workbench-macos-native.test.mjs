import { createHash } from "node:crypto";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { checkProduct, runNativeBatch } from "./workbench-macos-native.mjs";
import { createMatrix, validateMatrix } from "./workbench-macos-matrix.mjs";
const config = JSON.parse(readFileSync(new URL("../src-tauri/tauri.conf.json", import.meta.url)));
const product = config.identifier === "cn.ichain.agent.desktop" ? "ichain" : "hugagent";
const otherProduct = product === "ichain" ? "hugagent" : "ichain";
const cert = product === "ichain" ? "FDDD5A9A6FDBE2E306C147BBA6C9636DFBF9CC5C" : "0123456789ABCDEF0123456789ABCDEF01234567";
test("native release refuses product confusion and wrong pinned key", () => {
  assert.equal(checkProduct(config, product, config.plugins.updater.pubkey, cert).identifier, config.identifier);
  assert.throws(() => checkProduct(config, otherProduct, config.plugins.updater.pubkey, cert), /product/);
  assert.throws(() => checkProduct(config, product, config.plugins.updater.pubkey, "bad"), /certificate/);
  const other = structuredClone(config); other.identifier = "other";
  assert.throws(() => checkProduct(other, product, config.plugins.updater.pubkey, cert), /product/);
});
test("preparation cannot claim native acceptance or use Linux as Mac", async () => {
  const manifest = { sourceCommit: "a".repeat(40), architecture: "arm64", updaterTarget: "darwin-aarch64",
    version: "1.2.3", buildBatch: "20261014T120000Z-test", files: [{ path: "macos/app.app.tar.gz" }] };
  const bytes = Buffer.from(JSON.stringify(manifest));
  const report = createMatrix(manifest);
  assert.equal(report.rows.length, 95);
  assert.equal(report.rows.find(row => row.id === "local-ce-native-ipc").nativeIpc.transport, "");
  assert.ok(report.rows.every(row => row.status === "not-run"));
  assert.match(report.rows.find(row => row.id === "cloud-ce-install").expected, /no local backend/);
  assert.match(report.rows.find(row => row.id === "cloud-ee-offline").expected, /cannot start a local fallback/);
  assert.throws(() => validateMatrix(report, bytes, "."), /evidence/);
  report.manifestSha256 = createHash("sha256").update(bytes).digest("hex");
  report.isolatedAccount = "dedicated-test"; report.previousVersion = "1.2.2";
  report.device = { model: "fixture", macOS: "fixture", memoryGiB: 8, webview: "fixture" };
  assert.throws(() => validateMatrix(report, bytes, "."), /not passed/);
  for (const row of report.rows) { row.status = "pass"; row.actual = "fixture"; }
  assert.throws(() => validateMatrix(report, bytes, "."), /not passed/);
  assert.throws(() => createMatrix({ ...manifest, architecture: "x86_64" }), /native/);
  if (process.platform !== "darwin") await assert.rejects(runNativeBatch({}), /native macOS arm64/);
});
