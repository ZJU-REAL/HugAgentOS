import assert from "node:assert/strict";
import test from "node:test";
import { macosSigningPolicy, assertMacosSigning } from "./macos-signing.mjs";

const signed = {
  APPLE_SIGNING_IDENTITY: "Developer ID Application: Example Company (ABCDE12345)",
  APPLE_TEAM_ID: "ABCDE12345",
};
test("Mac packages require a stable signing identity by default", () => {
  assert.throws(() => macosSigningPolicy({}, "darwin"), /APPLE_SIGNING_IDENTITY/);
  assert.throws(() => macosSigningPolicy({ APPLE_SIGNING_IDENTITY: "-" }, "darwin"), /ad-hoc/);
  assert.equal(macosSigningPolicy(signed, "darwin").identity, signed.APPLE_SIGNING_IDENTITY);
});

test("local ad-hoc opt-in cannot weaken CI or release builds", () => {
  const env = { APPLE_SIGNING_IDENTITY: "-", HUGAGENT_ALLOW_ADHOC: "1" };
  assert.equal(macosSigningPolicy(env, "darwin").identity, "-");
  for (const required of [{ CI: "true" }, { HUGAGENT_RELEASE_BUILD: "1" }]) {
    assert.throws(() => macosSigningPolicy({ ...env, ...required }, "darwin"), /ad-hoc/);
  }
});
test("the configured team must match a Developer ID Application identity", () => {
  for (const env of [
    { ...signed, APPLE_TEAM_ID: "OTHER12345" },
    { ...signed, APPLE_TEAM_ID: "" },
    { ...signed, APPLE_SIGNING_IDENTITY: "Apple Development: Example (ABCDE12345)" },
  ]) assert.throws(() => macosSigningPolicy(env, "darwin"), /APPLE_TEAM_ID|Developer ID Application/);
});
test("release signing also requires complete notarization authentication", () => {
  const env = { ...signed, HUGAGENT_RELEASE_BUILD: "1" };
  assert.throws(() => macosSigningPolicy(env, "darwin"), /notarization/);
  assert.throws(() => macosSigningPolicy({ ...env, APPLE_API_KEY: "id" }, "darwin"), /notarization/);
  assert.equal(macosSigningPolicy({ ...env, APPLE_ID: "test@example.invalid", APPLE_PASSWORD: "fixture" }, "darwin").release, true);
  assert.equal(macosSigningPolicy({ ...env, APPLE_API_KEY: "id", APPLE_API_ISSUER: "issuer", APPLE_API_KEY_PATH: "/fixture/key.p8" }, "darwin").release, true);
});
test("Windows and Linux builds do not need Apple configuration", () => {
  for (const platform of ["win32", "linux"]) {
    assert.equal(assertMacosSigning({ CI: "true" }, platform, () => assert.fail("must not access the keychain")), null);
  }
});
test("an unavailable or locked build keychain fails without disclosing command output", () => {
  for (const response of [
    { status: 0, stdout: "0 valid identities found" },
    { status: 1, stdout: "", stderr: "sensitive-fixture" },
    { error: new Error("sensitive-fixture") },
  ]) {
    assert.throws(() => assertMacosSigning(signed, "darwin", () => response), (error) => {
      assert.match(error.message, /identity is unavailable/);
      assert.doesNotMatch(error.message, /sensitive-fixture/);
      return true;
    });
  }
  assert.equal(assertMacosSigning(signed, "darwin", () => ({
    status: 0, stdout: '1) FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF "' + signed.APPLE_SIGNING_IDENTITY + '"',
  })).identity, signed.APPLE_SIGNING_IDENTITY);
});

const selfSigned = {
  HUGAGENT_MACOS_SIGNING_MODE: "self-signed",
  APPLE_SIGNING_IDENTITY: "abcdef0123456789abcdef0123456789abcdef01",
};
test("explicit self-signed mode pins a certificate without paid-account credentials", () => {
  for (const flags of [{}, { CI: "true" }, { HUGAGENT_RELEASE_BUILD: "1" }]) {
    const policy = macosSigningPolicy({ ...selfSigned, ...flags }, "darwin");
    assert.equal(policy.mode, "self-signed");
    assert.equal(policy.certificateSha1, selfSigned.APPLE_SIGNING_IDENTITY.toUpperCase());
    assert.equal(policy.identity, policy.certificateSha1);
    assert.equal(policy.teamId, null);
  }
});
test("self-signed mode rejects names, malformed fingerprints, and implicit ad-hoc mode", () => {
  for (const identity of ["", "-", signed.APPLE_SIGNING_IDENTITY, "a".repeat(39), "g".repeat(40)]) {
    assert.throws(() => macosSigningPolicy({ ...selfSigned, APPLE_SIGNING_IDENTITY: identity }, "darwin"), /40.*hex|SHA-1/);
  }
  assert.throws(() => macosSigningPolicy({ ...signed, HUGAGENT_MACOS_SIGNING_MODE: "typo" }, "darwin"), /mode/);
});
test("self-signed preflight matches the exact fingerprint among untrusted identities", () => {
  const fingerprint = selfSigned.APPLE_SIGNING_IDENTITY.toUpperCase();
  const policy = assertMacosSigning(selfSigned, "darwin", (command, args) => {
    assert.equal(command, "/usr/bin/security");
    assert.deepEqual(args, ["find-identity", "-p", "codesigning"]);
    return { status: 0, stdout: '  1) ' + fingerprint + ' "Local Signing" (CSSMERR_TP_NOT_TRUSTED)' };
  });
  assert.equal(policy.certificateSha1, fingerprint);
  for (const stdout of [
    '1) ' + fingerprint + '0 "Wrong"',
    '1) 0' + fingerprint + ' "Wrong"',
    '1) ' + "F".repeat(40) + ' "' + fingerprint + '"',
    "0 identities found",
  ]) {
    assert.throws(() => assertMacosSigning(selfSigned, "darwin", () => ({ status: 0, stdout })), /identity is unavailable/);
  }
  for (const result of [{ status: 1, stderr: "sensitive-fixture" }, { error: new Error("sensitive-fixture") }]) {
    assert.throws(() => assertMacosSigning(selfSigned, "darwin", () => result), (error) => {
      assert.doesNotMatch(error.message, /sensitive-fixture/);
      return /identity is unavailable/.test(error.message);
    });
  }
});
