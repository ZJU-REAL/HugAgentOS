import assert from "node:assert/strict";
import test from "node:test";
import { compareVersions, selectUosRelease } from "../src/updater.mjs";

test("update comparison is numeric, not lexical", () => {
  assert.equal(compareVersions("0.2.10", "0.2.9"), 1);
  assert.equal(compareVersions("v1.0.0", "1.0"), 0);
  assert.equal(compareVersions("0.2.8", "0.2.9"), -1);
});

test("UOS updater selects only aarch64 platform entries", () => {
  const release = { url: "HugAgentOS.deb", sha256: "a".repeat(64) };
  assert.equal(selectUosRelease({ platforms: { "linux-x86_64": {}, "linux-aarch64": release } }), release);
  assert.equal(selectUosRelease({ platforms: { "linux-x86_64": {} } }), null);
});
