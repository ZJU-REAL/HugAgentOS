import { spawnSync } from "node:child_process";
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

export function macosSigningPolicy(env = process.env, platform = process.platform) {
  if (platform !== "darwin") return null;
  const identity = env.APPLE_SIGNING_IDENTITY?.trim();
  const mode = env.HUGAGENT_MACOS_SIGNING_MODE?.trim() || "developer-id";
  if (!["developer-id", "self-signed"].includes(mode)) {
    throw new Error("Unknown macOS signing mode; use developer-id or self-signed.");
  }
  const release = env.HUGAGENT_RELEASE_BUILD === "1" || Boolean(env.CI && env.CI !== "false");
  if (mode === "self-signed") {
    if (!/^[A-Fa-f0-9]{40}$/.test(identity || "")) {
      throw new Error("Self-signed mode requires APPLE_SIGNING_IDENTITY to be the 40-hex certificate SHA-1 fingerprint.");
    }
    const certificateSha1 = identity.toUpperCase();
    return { mode, identity: certificateSha1, certificateSha1, teamId: null, release };
  }
  if (identity === "-") {
    if (env.HUGAGENT_ALLOW_ADHOC !== "1" || release) {
      throw new Error("macOS ad-hoc signing is only allowed for local tests: set " +
        "HUGAGENT_ALLOW_ADHOC=1 and APPLE_SIGNING_IDENTITY=- without CI or HUGAGENT_RELEASE_BUILD=1.");
    }
    return { mode: "ad-hoc", identity, certificateSha1: null, teamId: null, release: false };
  }
  if (!identity) {
    throw new Error("APPLE_SIGNING_IDENTITY is required for macOS packages. " +
      "Use the persistent self-signed helper (setup-macos-self-signing.py), or install a Developer ID identity.");
  }
  const match = /^Developer ID Application: .+ \(([A-Z0-9]{10})\)$/.exec(identity);
  if (!match) throw new Error("APPLE_SIGNING_IDENTITY must be the full Developer ID Application certificate name.");
  const teamId = env.APPLE_TEAM_ID?.trim();
  if (!teamId || teamId !== match[1]) {
    throw new Error("APPLE_TEAM_ID must match the Developer ID Application signing identity.");
  }
  if (release) {
    const appleId = env.APPLE_ID?.trim() && env.APPLE_PASSWORD?.trim();
    const apiKey = env.APPLE_API_KEY?.trim() && env.APPLE_API_ISSUER?.trim() && env.APPLE_API_KEY_PATH?.trim();
    if (!appleId && !apiKey) {
      throw new Error("macOS release notarization requires APPLE_ID/APPLE_PASSWORD or " +
        "APPLE_API_KEY/APPLE_API_ISSUER/APPLE_API_KEY_PATH. No credentials are printed.");
    }
  }
  return { mode, identity, certificateSha1: null, teamId, release };
}

export function assertMacosSigning(
  env = process.env, platform = process.platform, run = spawnSync,
) {
  const policy = macosSigningPolicy(env, platform);
  if (!policy || policy.identity === "-") return policy;
  // Self-signed identities need not be trusted by Apple's certificate chain.
  const args = policy.mode === "self-signed"
    ? ["find-identity", "-p", "codesigning"]
    : ["find-identity", "-v", "-p", "codesigning"];
  const result = run("/usr/bin/security", args, { encoding: "utf8", shell: false });
  const identities = [...(result.stdout || "").matchAll(/^\s*\d+\)\s+([A-Fa-f0-9]{40})\s+"([^"\r\n]+)"/gm)];
  const found = identities.some((match) => policy.mode === "self-signed"
    ? match[1].toUpperCase() === policy.certificateSha1
    : match[2] === policy.identity);
  if (result.error || result.status !== 0 || !found) {
    throw new Error("The requested macOS signing identity is unavailable. " +
      "Import its certificate and private key into the build user's keychain and unlock it before building.");
  }
  return policy;
}

if (process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))) {
  try {
    const policy = assertMacosSigning();
    console.log(policy?.identity === "-" ? "[desktop] Local-test ad-hoc signing only." :
      "[desktop] macOS signing prerequisites verified.");
  } catch (error) {
    console.error("[desktop] " + error.message);
    process.exitCode = 1;
  }
}
