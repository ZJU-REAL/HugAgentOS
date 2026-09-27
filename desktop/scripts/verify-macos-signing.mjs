import { spawnSync } from "node:child_process";
import { readFileSync, realpathSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

// Inspect public signing metadata only. Never read application Keychain items.
export function verifyMacosApp({
  app, identifier, teamId, mode = "developer-id", certificateSha1,
}, run = spawnSync) {
  if (!["developer-id", "self-signed"].includes(mode)) throw new Error("Unknown macOS signing mode.");
  if (!app || !identifier) throw new Error("An app path and expected bundle identifier are required.");
  if (mode === "developer-id" && !/^[A-Z0-9]{10}$/.test(teamId || "")) {
    throw new Error("A 10-character Apple Team ID is required for Developer ID verification.");
  }
  if (mode === "self-signed" && !/^[A-Fa-f0-9]{40}$/.test(certificateSha1 || "")) {
    throw new Error("Self-signed verification requires the pinned certificate's 40-character SHA-1 fingerprint.");
  }
  const invoke = (command, args, message) => {
    const result = run(command, args, { encoding: "utf8", shell: false });
    if (result.error || result.status !== 0) throw new Error(message);
    return (result.stdout || "") + (result.stderr || "");
  };
  const metadata = invoke("/usr/bin/codesign", ["--display", "--verbose=4", "--requirements", "-", app],
    "Cannot inspect the application signature.");
  if (/^Signature=adhoc$/m.test(metadata) || /flags=.*\badhoc\b/m.test(metadata)) {
    throw new Error("Release rejected: ad-hoc signing cannot preserve Keychain authorization across upgrades.");
  }
  if (!metadata.split("\n").includes("Identifier=" + identifier)) {
    throw new Error("Release rejected: the application bundle identifier changed.");
  }
  const requirement = /^#? ?designated => (.+)$/m.exec(metadata)?.[1];
  if (!requirement || /\bcdhash\b/.test(requirement)) {
    throw new Error("Release rejected: no stable designated requirement.");
  }
  if (mode === "self-signed") {
    // Require the complete pinned identity, not a substring that an OR clause
    // or a version-specific condition could weaken or invalidate on upgrade.
    const pinned = /^identifier ("(?:[^"\\]|\\.)*") and (?:anchor|certificate leaf =) H"([a-fA-F0-9]{40})"$/.exec(requirement);
    if (!pinned || pinned[1] !== JSON.stringify(identifier) ||
        pinned[2].toUpperCase() !== certificateSha1.toUpperCase()) {
      throw new Error("Release rejected: designated requirement must bind only the identifier and pinned certificate.");
    }
  } else if (!metadata.split("\n").includes("TeamIdentifier=" + teamId) ||
      !/^Authority=Developer ID Application: /m.test(metadata) ||
      !requirement.includes('identifier "' + identifier + '"') ||
      !requirement.includes("anchor apple generic") ||
      !requirement.includes("certificate leaf[subject.OU]")) {
    throw new Error("Release rejected: expected stable Developer ID Application identity does not match.");
  }
  invoke("/usr/bin/codesign", ["--verify", "--deep", "--strict", app],
    "Release rejected: application signature or sealed resources are invalid.");
  if (mode === "self-signed") {
    const pin = certificateSha1.toUpperCase();
    // The leading "=" selects an inline requirement instead of a file path.
    invoke("/usr/bin/codesign", ["--verify", "--strict", "-R", '=certificate leaf = H"' + pin + '"', app],
      "Release rejected: application was not signed by the pinned certificate.");
    return { identifier, mode, certificateSha1: pin, designatedRequirement: requirement, appleNotarized: false };
  }
  invoke("/usr/bin/xcrun", ["stapler", "validate", app],
    "Release rejected: no valid stapled notarization ticket.");
  invoke("/usr/sbin/spctl", ["--assess", "--type", "execute", app],
    "Release rejected: Gatekeeper did not accept the notarized application.");
  return { identifier, mode, teamId, designatedRequirement: requirement, appleNotarized: true };
}

if (process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))) {
  try {
    if (process.platform !== "darwin") throw new Error("Run this verification on the native Mac builder.");
    const { values } = parseArgs({ options: {
      app: { type: "string" }, "bundle-dir": { type: "string" },
      identifier: { type: "string" }, "team-id": { type: "string" },
      mode: { type: "string" }, "certificate-sha1": { type: "string" },
    } });
    if (Boolean(values.app) === Boolean(values["bundle-dir"])) {
      throw new Error("Specify exactly one of --app or --bundle-dir.");
    }
    const config = values["bundle-dir"] || !values.identifier
      ? JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../src-tauri/tauri.conf.json"), "utf8"))
      : null;
    const app = resolve(values.app || join(values["bundle-dir"], config.productName + ".app"));
    const result = verifyMacosApp({
      app, identifier: values.identifier || config.identifier,
      teamId: values["team-id"] || process.env.APPLE_TEAM_ID,
      mode: values.mode || process.env.HUGAGENT_MACOS_SIGNING_MODE || "developer-id",
      certificateSha1: values["certificate-sha1"] || process.env.APPLE_SIGNING_IDENTITY,
    });
    console.log(JSON.stringify({ app, ...result }, null, 2));
  } catch (error) {
    console.error("[desktop] " + error.message);
    process.exitCode = 1;
  }
}
