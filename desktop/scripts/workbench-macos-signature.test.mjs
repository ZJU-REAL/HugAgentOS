import test from "node:test";
import assert from "node:assert/strict";
import { verifyUpdaterBytes } from "./workbench-macos-signature.mjs";
// Known minisign-verify 0.2.5 compatibility vector (also used by the desktop updater).
const pubkey = "RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3";
const signature = "untrusted comment: signature from minisign secret key\nRWQf6LRCGA9i59SLOFxz6NxvASXDJeRtuZykwQepbDEGt87ig1BNpWaVWuNrm73YiIiJbq71Wi+dP9eKL8OC351vwIasSSbXxwA=\ntrusted comment: timestamp:1555779966\tfile:test\nQtKMXWyYcwdpZAlPF7tE2ENJkRd1ujvKjlj1m9RtHTBnZPa5WKU5uWRs5GoP5M/VqE81QFuMKI5k/SfNQUaOAA==";
test("updater verifies artifact and authenticated comment with trusted product key", () => {
  assert.equal(verifyUpdaterBytes(Buffer.from("test"), signature, pubkey).keyId, "E7620F1842B4E81F");
  assert.throws(() => verifyUpdaterBytes(Buffer.from("Test"), signature, pubkey), /signature/);
  assert.throws(() => verifyUpdaterBytes(Buffer.from("test"), signature.replace("file:test", "file:other"), pubkey), /comment/);
  const wrong = Buffer.from(pubkey, "base64"); wrong[2] ^= 1;
  assert.throws(() => verifyUpdaterBytes(Buffer.from("test"), signature, wrong.toString("base64")), /key/);
});

const prehashed = "untrusted comment: signature from minisign secret key\nRUQf6LRCGA9i559r3g7V1qNyJDApGip8MfqcadIgT9CuhV3EMhHoN1mGTkUidF/z7SrlQgXdy8ofjb7bNJJylDOocrCo8KLzZwo=\ntrusted comment: timestamp:1556193335\tfile:test\ny/rUw2y8/hOUYjZU71eHp/Wo1KZ40fGy2VJEDl34XMJM+TX48Ss/17u3IvIfbVR1FkZZSNCisQbuQY+bHwhEBg==";
test("prehashed native update signatures use Blake2b and reject changed archive bytes", () => {
  assert.equal(verifyUpdaterBytes(Buffer.from("test"), prehashed, pubkey).algorithm, "ED");
  assert.throws(() => verifyUpdaterBytes(Buffer.from("Test"), prehashed, pubkey), /signature/);
});
