import assert from "node:assert/strict";
import test from "node:test";
import { safeLink, safeRelative, validIdentifier } from "../src/local-payload.mjs";
import { isProxyPath, sanitizedHeaders } from "../src/proxy.mjs";

test("payload extraction rejects path traversal and escaping links", () => {
  for (const value of ["../escape", "/absolute", "a/../../escape", "C:\\escape"]) {
    assert.throws(() => safeRelative(value));
  }
  assert.equal(safeRelative("python/bin/python3.11"), "python/bin/python3.11");
  assert.throws(() => safeLink("python/bin/link", "../../../escape"));
  assert.equal(safeLink("python/bin/link", "../lib/python"), true);
  assert.equal(validIdentifier("a".repeat(64)), true);
  assert.equal(validIdentifier("../not-a-hash"), false);
});

test("proxy strips credentials and desktop routing headers from the renderer", () => {
  const headers = sanitizedHeaders({
    host: "127.0.0.1:1234",
    cookie: "forged=1",
    connection: "keep-alive",
    "x-hugagent-target": "local",
    "x-desktop-bridge": "forged",
    "x-desktop-bridge-user": "forged",
    "content-type": "application/json",
  });
  assert.deepEqual(headers, { "content-type": "application/json", "accept-encoding": "identity" });
  assert.equal(isProxyPath("/api/v1/chats/stream"), true);
  assert.equal(isProxyPath("/files/report.pdf"), true);
  assert.equal(isProxyPath("/not-an-api"), false);
});
