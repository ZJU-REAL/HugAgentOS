import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { configureUosRuntime, startWhenReady } from "../src/lifecycle.mjs";

test("UOS startup disables hardware acceleration before Electron becomes ready", () => {
  let disabled = false;

  configureUosRuntime({
    disableHardwareAcceleration: () => { disabled = true; },
  });

  assert.equal(disabled, true);
});

test("the packaged UOS launcher selects X11 before Electron starts", async () => {
  const packageJson = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));

  assert.deepEqual(packageJson.build.linux.executableArgs, ["--ozone-platform=x11"]);
});

test("startup is scheduled without blocking the Electron main module", async () => {
  let resolveReady;
  const ready = new Promise((resolve) => { resolveReady = resolve; });
  const events = [];

  const result = startWhenReady({
    app: { whenReady: () => ready },
    initialize: async () => { events.push("initialized"); },
    onError: (error) => { throw error; },
  });

  assert.equal(result, undefined);
  assert.deepEqual(events, []);

  resolveReady();
  await ready;
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(events, ["initialized"]);
});

test("startup failures are routed to the supplied handler", async () => {
  const failure = new Error("initialization failed");
  let reported = null;

  startWhenReady({
    app: { whenReady: () => Promise.resolve() },
    initialize: async () => { throw failure; },
    onError: (error) => { reported = error; },
  });

  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(reported, failure);
});
