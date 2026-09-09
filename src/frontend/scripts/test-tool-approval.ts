import assert from "node:assert/strict";
import { setHybridDual, getToolApprovalMode, setToolApprovalMode } from "../src/api";
let cloud = "ask";
let local = "ask";
let failLocal = false;
let failCloud = false;
const calls: string[] = [];
globalThis.fetch = async (_url, init) => {
  const isLocal = new Headers(init?.headers).get("x-hugagent-target") === "local";
  const method = init?.method || "GET";
  calls.push(`${isLocal ? "local" : "cloud"}:${method}`);
  if (isLocal ? failLocal : failCloud) return new Response(JSON.stringify({ message: "unavailable" }), { status: 503 });
  if (method === "PUT") {
    const mode = JSON.parse(String(init?.body)).mode;
    if (isLocal) local = mode; else cloud = mode;
  }
  return new Response(JSON.stringify({ code: 200, data: { mode: isLocal ? local : cloud } }));
};
setHybridDual(true);
await setToolApprovalMode("full");
assert.equal(cloud, "full");
assert.equal(local, "full", "full access must reach the host executor");
local = "ask";
assert.equal(await getToolApprovalMode(), "full");
assert.equal(local, "full", "reopening desktop repairs a previously cloud-only preference");
for (const mode of ["ask", "auto", "full"] as const) {
  await setToolApprovalMode(mode);
  assert.equal(local, mode);
  assert.equal(cloud, mode);
}
failLocal = true;
await assert.rejects(setToolApprovalMode("ask"));
await assert.rejects(getToolApprovalMode(), "must not display full access when the host cannot be checked");
failLocal = false;
await getToolApprovalMode();
assert.equal(local, "ask", "retry repairs a partially saved preference");
failCloud = true;
calls.length = 0;
await assert.rejects(setToolApprovalMode("full"));
assert.equal(local, "ask", "cloud failure must not silently expand local permissions");
assert.deepEqual(calls, ["cloud:PUT"]);
failCloud = false;
// A delayed startup read must finish before a newer user choice is saved.
const fetchNormally = globalThis.fetch;
let releaseRead!: () => void;
const delayed = new Promise<void>((resolve) => { releaseRead = resolve; });
let markStarted!: () => void;
const started = new Promise<void>((resolve) => { markStarted = resolve; });
let delayNextRead = true;
globalThis.fetch = async (url, init) => {
  if (delayNextRead && (!init?.method || init.method === "GET")) {
    delayNextRead = false;
    markStarted();
    await delayed;
  }
  return fetchNormally(url, init);
};
const reading = getToolApprovalMode();
await started;
const saving = setToolApprovalMode("full");
releaseRead();
await Promise.all([reading, saving]);
assert.equal(cloud, "full");
assert.equal(local, "full", "late startup repair must not overwrite a newer selection");
globalThis.fetch = fetchNormally;
setHybridDual(false);
calls.length = 0;
await setToolApprovalMode("auto");
assert.equal(await getToolApprovalMode(), "auto");
assert.deepEqual(calls, ["cloud:PUT", "cloud:GET"], "single-backend installations must not duplicate requests");
console.log("tool approval: dual sync, startup repair, failures, retries and single-backend controls passed");
