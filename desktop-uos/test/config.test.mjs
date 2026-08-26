import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { cloudBase, loadConfig, provision, provisionMode } from "../src/config.mjs";

test("dual mode remains cloud-primary and remembers the local execution plane", async () => {
  const dir = await mkdtemp(join(tmpdir(), "hugagent-uos-config-"));
  try {
    await provision(dir, "dual", "https://cloud.example.test/");
    const config = await loadConfig(dir);
    assert.equal(config.deployment_mode, "remote");
    assert.equal(config.server_base, "https://cloud.example.test");
    assert.equal(cloudBase(config), "https://cloud.example.test");
    assert.equal(provisionMode(config), "dual");
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test("cloud mode rejects non-http server addresses", async () => {
  const dir = await mkdtemp(join(tmpdir(), "hugagent-uos-config-"));
  try {
    await assert.rejects(() => provision(dir, "cloud_only", "file:///etc/passwd"));
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
