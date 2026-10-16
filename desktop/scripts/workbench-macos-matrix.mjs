import { createHash } from "node:crypto";
import { existsSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { emptyNativeIpcProof, validateNativeIpcProof } from "./workbench-macos-ipc-evidence.mjs";

export const scenarios = [
  ["install", "Install DMG app in dedicated macOS test account; first launch and local health/ready succeed without PyPI."],
  ["protocol", "Open UI v3 workspace; negotiate v1.0.0, reject wrong nonce/version, retain v1/v2 canvas/tool results."],
  ["navigation", "Pin/search/order, both sidebars, child route, refresh/deep-link/back/forward; keyboard focus and theme work."],
  ["source", "Same slug in local/cloud installations remains separate; revoked/missing cloud origin never falls back locally."],
  ["files", "Host-confirmed native pick/save, cancel, Chinese and spaced paths; scoped ownership on each read/write; no plugin native invoke."],
  ["external", "Confirmed https external link opens system browser; unsafe schemes and forged gesture fail; denied embed offers browser fallback."],
  ["offline", "Disconnect test network; installed local app works, cloud-dependent app explains failure; reconnect restores only correct origin."],
  ["identity", "A opens app/data, logout/switch B; old channels and late responses cannot expose A data; login cancellation stays cancelled."],
  ["permission", "Wrong user/install/revision, revoked grants and expanded upgrade permissions fail; new launch/data/resource always authorize."],
  ["native-ipc", "In the actual installed package, srcdoc and external iframe attempts to old logout/login and Core event/window IPC receive permission denials; host session/windows/events stay unchanged. Real host logout/login, menus, updater, window controls and dragging still work."],
  ["lifecycle", "100 unopened apps spawn no iframe/process; dirty switch prompt, crash/timeout isolated; close/uninstall/logout releases leases."],
  ["upgrade", "Old signed client with fixture data upgrades to new signed package; stable Keychain identity, source/runtime fingerprints checked."],
  ["recovery", "Faulted isolated local upgrade restores source/runtime/database/files; no deletion to bypass failure."],
  ["keychain", "Locked/refused Keychain uses in-memory session, no plaintext credentials; logout prevents old account resurrection."],
  ["windows", "Native system menus, traffic lights, drag, resize/zoom, multiwindow, Dock/tray close and quickask preserve behavior."],
  ["login-chat", "Login/new/select/search/sort/favorite/delete, paging, stream/cancel/reconnect/edit/regenerate/share, draft and attachment flow."],
  ["projects-space", "Personal/team project authorization, folder binding/instructions/files/chat; space move/import/download/delete/favorite/save conflicts."],
  ["execution", "Plan/batch confirm/cancel/resume; isolated automation task/history/notification; MCP/skills/sandbox artifacts and resource cleanup."],
  ["legacy", "Canvas tabs/preview/edit, plugin canvas/tool result; memory availability; old application data/MCP/sites/admin/config/API/old links retain semantics."],
];
export const combinations = [
  { mode: "local", serverEdition: "ce" }, { mode: "cloud", serverEdition: "ce" },
  { mode: "cloud", serverEdition: "ee" }, { mode: "dual", serverEdition: "ce" },
  { mode: "dual", serverEdition: "ee" },
];
function expectation(id, combo, steps) {
  if (id === "install" && combo.mode === "cloud") {
    return "Install actual DMG, select cloud mode and log in; no local backend process starts.";
  }
  if (id === "source" && combo.mode !== "dual") {
    return "Only the selected origin is available; forged foreign installation bindings fail without fallback.";
  }
  if (id === "offline" && combo.mode === "cloud") {
    return "Offline cloud app reports origin unavailable, cannot start a local fallback; reconnect uses the correct account.";
  }
  if (id === "recovery" && combo.mode === "cloud") {
    return "Failed isolated desktop update retains old cloud client configuration/session identity; local backend stays inactive.";
  }
  if (id === "legacy" || id === "projects-space" || id === "execution") {
    return steps + " Unavailable edition/runtime capabilities report explicit reasons without gaining privileges.";
  }
  return steps;
}
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
export function createMatrix(manifest) {
  if (!/^[a-f0-9]{40}$/.test(manifest.sourceCommit || "") || manifest.architecture !== "arm64" ||
      manifest.updaterTarget !== "darwin-aarch64" || !manifest.files?.length) throw new Error("Expected native build manifest.");
  return { schemaVersion: 1, issue: "WB-25", platform: "macOS", level: "native",
    sourceCommit: manifest.sourceCommit, version: manifest.version, buildBatch: manifest.buildBatch,
    manifestSha256: "", device: { model: "", macOS: "", memoryGiB: "", webview: "" },
    isolatedAccount: "", previousVersion: "", rows: combinations.flatMap(combo => scenarios.map(([id, steps]) => ({
      id: combo.mode + "-" + combo.serverEdition + "-" + id, ...combo, acceptance: id === "native-ipc" ? "A08,A13" : "A13", steps,
      ...(id === "native-ipc" ? { nativeIpc: emptyNativeIpcProof() } : {}),
      expected: expectation(id, combo, steps), actual: "", status: "not-run", evidence: [], issue: "",
    }))) };
}
export function validateMatrix(report, manifestBytes, root) {
  const manifest = JSON.parse(manifestBytes);
  const expected = createMatrix(manifest);
  if (report.platform !== "macOS" || report.level !== "native" ||
      report.sourceCommit !== manifest.sourceCommit || report.version !== manifest.version ||
      report.buildBatch !== manifest.buildBatch || report.manifestSha256 !== hash(manifestBytes) ||
      !report.isolatedAccount || !report.previousVersion ||
      ["model", "macOS", "memoryGiB", "webview"].some(key => !report.device?.[key])) {
    throw new Error("Missing native device, source, isolation, or upgrade evidence.");
  }
  if (report.rows?.length !== expected.rows.length) throw new Error("Incomplete native acceptance matrix.");
  const seen = new Set();
  for (const row of expected.rows) {
    const actual = report.rows.find(item => item.id === row.id);
    if (!actual || seen.has(actual.id) || actual.mode !== row.mode || actual.serverEdition !== row.serverEdition ||
        actual.status !== "pass" || !actual.actual?.trim() || !actual.evidence?.length) {
      throw new Error("Native case not passed: " + row.id);
    }
    if (row.id.endsWith("-native-ipc")) {
      validateNativeIpcProof(actual.nativeIpc);
      if (!manifest.files.some(file => file.sha256 === actual.nativeIpc.installedPackageSha256 &&
          /(?:\.dmg|\.app\.tar\.gz)$/.test(file.path))) {
        throw new Error("Native IPC proof refers to another installed package.");
      }
    }
    seen.add(actual.id);
    for (const evidence of actual.evidence) {
      if (typeof evidence.path !== "string" || isAbsolute(evidence.path)) throw new Error("Use relative evidence paths.");
      const file = realpathSync(resolve(root, evidence.path));
      const rel = relative(realpathSync(root), file);
      if (rel.startsWith("..") || isAbsolute(rel) || hash(readFileSync(file)) !== evidence.sha256) {
        throw new Error("Evidence path or SHA-256 mismatch.");
      }
    }
  }
  return { status: "pass", level: "native", rows: seen.size, sourceCommit: report.sourceCommit };
}

if (process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))) {
  try {
    const { values } = parseArgs({ options: {
      manifest: { type: "string" }, output: { type: "string" }, report: { type: "string" },
    } });
    const bytes = readFileSync(values.manifest);
    if (values.report) {
      const file = resolve(values.report);
      console.log(JSON.stringify(validateMatrix(JSON.parse(readFileSync(file)), bytes, dirname(file))));
    } else {
      if (!values.output || existsSync(values.output)) throw new Error("Specify new --output; never overwrite evidence.");
      const report = createMatrix(JSON.parse(bytes)); report.manifestSha256 = hash(bytes);
      writeFileSync(values.output, JSON.stringify(report, null, 2) + "\n", { flag: "wx", mode: 0o600 });
      console.log("Native matrix initialized: all cases not-run.");
    }
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
