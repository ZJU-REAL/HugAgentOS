import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import test from "node:test";
import { verifyMacosApp } from "./verify-macos-signing.mjs";
const options = { app: "/fixture/Example.app", identifier: "com.example.desktop", teamId: "ABCDE12345" };
const good = `Identifier=com.example.desktop
Signature size=1234
Authority=Developer ID Application: Example Company (ABCDE12345)
TeamIdentifier=ABCDE12345
designated => identifier "com.example.desktop" and anchor apple generic and certificate leaf[subject.OU] = ABCDE12345`;
function commands(metadata = good, fail = "") {
  return (command, args) => {
    const key = command.split("/").pop();
    if (key === "codesign" && args.includes("--display")) return { status: 0, stderr: metadata };
    return { status: key === fail ? 1 : 0, stderr: "" };
  };
}
test("release verification rejects ad-hoc applications like the existing Mac packages", () => {
  assert.throws(() => verifyMacosApp(options, commands(
    'Identifier=com.example.desktop\nSignature=adhoc\nTeamIdentifier=not set\n# designated => cdhash H"abc"'
  )), /ad-hoc/);
});

test("a notarized app with stable identity passes the release check", () => {
  const result = verifyMacosApp(options, commands());
  assert.equal(result.identifier, options.identifier);
  assert.equal(result.appleNotarized, true);
});
test("release verification rejects changed identities and hash-bound requirements", () => {
  for (const metadata of [
    good.replace("Identifier=com.example.desktop", "Identifier=com.other.desktop"),
    good.replace("TeamIdentifier=ABCDE12345", "TeamIdentifier=OTHER12345"),
    good.replace("Authority=Developer ID Application:", "Authority=Apple Development:"),
    good.replace(/designated => .*/, ''),
    good.replace(/designated => .*/, 'designated => identifier "com.example.desktop" and anchor apple generic and certificate leaf[subject.OU] = ABCDE12345 and cdhash H"abc"'),
  ]) {
    assert.throws(() => verifyMacosApp(options, commands(metadata)), /Release rejected/);
  }
});
test("a broken seal or missing notarization fails verification", () => {
  for (const command of ["codesign", "xcrun", "spctl"]) {
    assert.throws(() => verifyMacosApp(options, commands(good, command)), /Release rejected/);
  }
});

test("the verifier cannot silently skip checks when invoked through a symlink", { skip: process.platform === "win32" }, () => {
  const dir = mkdtempSync(join(tmpdir(), "mac-signing-cli-"));
  try {
    const link = join(dir, "verify.mjs");
    symlinkSync(fileURLToPath(new URL("./verify-macos-signing.mjs", import.meta.url)), link);
    const result = spawnSync(process.execPath, [link], { encoding: "utf8" });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /native Mac builder|Specify exactly one/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

const certificateSha1 = "0123456789ABCDEF0123456789ABCDEF01234567";
const selfSigned = { app: options.app, identifier: options.identifier, mode: "self-signed", certificateSha1 };
const selfSignedMetadata = `Identifier=com.example.desktop
Authority=Example Desktop Signing
TeamIdentifier=not set
designated => identifier "com.example.desktop" and anchor H"${certificateSha1.toLowerCase()}"`;
test("self-signed verification pins the certificate without claiming Apple notarization", () => {
  const calls = [];
  const result = verifyMacosApp(selfSigned, (command, args) => {
    calls.push([command, args]);
    return commands(selfSignedMetadata)(command, args);
  });
  assert.equal(result.appleNotarized, false);
  assert.equal(result.certificateSha1, certificateSha1);
  assert.equal(result.mode, "self-signed");
  assert.ok(calls.every(([command]) => command === "/usr/bin/codesign"));
  assert.ok(calls.some(([, args]) => args.includes("--deep") && args.includes("--strict")));
  assert.ok(calls.some(([, args]) => args.includes("-R") && args.includes('=certificate leaf = H"' + certificateSha1 + '"')));
});
test("self-signed verification rejects missing, incorrect and unstable certificate requirements", () => {
  assert.throws(() => verifyMacosApp({ ...selfSigned, certificateSha1: "" }, commands(selfSignedMetadata)), /SHA-1/);
  assert.throws(() => verifyMacosApp({ ...selfSigned, mode: "unknown" }, commands(selfSignedMetadata)), /mode/);
  for (const metadata of [
    selfSignedMetadata.replace(certificateSha1.toLowerCase(), "a".repeat(40)),
    selfSignedMetadata + ' and cdhash H"abc"',
    selfSignedMetadata + ' or true',
    selfSignedMetadata.replace('identifier "com.example.desktop"', 'identifier "com.other.desktop"'),
    selfSignedMetadata.replace(/designated => .*/, ""),
  ]) assert.throws(() => verifyMacosApp(selfSigned, commands(metadata)), /Release rejected/);
});
test("self-signed verification rejects a real certificate mismatch despite plausible metadata", () => {
  assert.throws(() => verifyMacosApp(selfSigned, (command, args) =>
    args.includes("-R") ? { status: 1 } : commands(selfSignedMetadata)(command, args)
  ), /certificate/);
});

test("native self-signed certificate leaf requirements retain the exact signing pin", () => {
  const metadata = selfSignedMetadata.replace("and anchor H", "and certificate leaf = H");
  assert.equal(verifyMacosApp(selfSigned, commands(metadata)).certificateSha1, certificateSha1);
  for (const invalid of [
    metadata.replace(certificateSha1.toLowerCase(), "a".repeat(40)),
    metadata + " or true",
    metadata + ' and cdhash H"abc"',
  ]) assert.throws(() => verifyMacosApp(selfSigned, commands(invalid)), /Release rejected/);
});
