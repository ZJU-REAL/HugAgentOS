import test from "node:test";
import assert from "node:assert/strict";
import { emptyNativeIpcProof, validateNativeIpcProof } from "./workbench-macos-ipc-evidence.mjs";

test("code absence, missing API or HTML-only results cannot certify native IPC isolation", () => {
  const proof = emptyNativeIpcProof();
  assert.equal(proof.probes.length, 16);
  assert.throws(() => validateNativeIpcProof(proof), /actual package/);
  Object.assign(proof, { transport: "actual-package-ipc", installedPackageSha256: "a".repeat(64),
    hostSessionSentinel: "test-account-epoch", hostWindowSentinel: "test-window-state",
    sessionUnchanged: true, windowUnchanged: true, eventDeliveryCount: 0 });
  for (const probe of proof.probes) Object.assign(probe, {
    status: "denied", validPayload: true, denialKind: "permission", ipcResponseCode: "fixture-rejected",
    ipcResponse: "checker fixture only; not an actual native response",
  });
  for (const action of Object.keys(proof.hostRegressions)) proof.hostRegressions[action] = "pass";
  assert.doesNotThrow(() => validateNativeIpcProof(proof));
  proof.probes.find(probe => probe.command === "plugin:window|close").denialKind = "removed";
  assert.throws(() => validateNativeIpcProof(proof), /not denied/);
});
test("host session effects and broken legitimate host actions fail native acceptance", () => {
  const proof = emptyNativeIpcProof();
  proof.sessionUnchanged = false;
  assert.throws(() => validateNativeIpcProof(proof), /sentinels/);
});
