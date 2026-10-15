import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { captureSource } from "./workbench-source-snapshot.mjs";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const actions = { "rust-check": "check", "rust-test": "test", "rust-build": "build" };
export function validationEnvironment(env, batchDir) {
  if (env.HUGAGENT_RELEASE_BUILD === "1" || (env.CI && env.CI !== "false")) {
    throw new Error("Use the unchanged release pipeline for release/CI builds.");
  }
  if (env.TAURI_CONFIG || env.CARGO_BUILD_TARGET || env.HUGAGENT_ALLOW_ADHOC) {
    throw new Error("Remove conflicting validation overrides.");
  }
  const result = { ...env, CARGO_TARGET_DIR: join(batchDir, "target"), TMPDIR: join(batchDir, "tmp"),
    TAURI_CONFIG: JSON.stringify({ bundle: { resources: [] } }) };
  // Rust source checks have no signing stage. Do not pass signing secrets into them.
  for (const name of Object.keys(result)) {
    if (/^(TAURI_(SIGNING_)?PRIVATE_KEY|APPLE_(PASSWORD|API_KEY|API_ISSUER|ID))/.test(name)) delete result[name];
  }
  return result;
}

export async function runValidation(values) {
  if (!["snapshot", ...Object.keys(actions)].includes(values.action)) throw new Error("Unknown validation action.");
  const root = realpathSync(values.checkout);
  if (realpathSync(join(scriptDir, "..", "..")) !== root) throw new Error("Run validation from the specified source tree.");
  const before = await captureSource(root);
  if (values.action !== "snapshot") {
    if (process.platform !== "darwin" || process.arch !== "arm64") throw new Error("Rust validation requires native macOS arm64.");
    if (values["baseline-commit"] !== before.baselineCommit || values["source-sha256"] !== before.sourceSha256) {
      throw new Error("Complete validation source does not match the pinned baseline/tree SHA-256.");
    }
  }
  if (!/^[0-9]{8}T[0-9]{6}Z-[a-z0-9-]+$/.test(values.batch || "")) throw new Error("Use a unique validation batch.");
  const batchDir = join(root, ".scratch/workbench-25", values.batch);
  if (existsSync(batchDir)) throw new Error("Validation batch already exists.");
  for (const dir of [join(root, ".scratch"), join(root, ".scratch/workbench-25")]) {
    if (existsSync(dir)) {
      const rel = relative(root, realpathSync(dir));
      if (rel.startsWith("..") || isAbsolute(rel)) throw new Error("Validation parent escapes checkout.");
    }
  }
  mkdirSync(batchDir, { recursive: true, mode: 0o700 });
  const rel = relative(root, realpathSync(batchDir));
  if (rel.startsWith("..") || isAbsolute(rel)) throw new Error("Validation output escapes checkout.");
  writeFileSync(join(batchDir, "source-before.json"), JSON.stringify(before, null, 2) + "\n", { flag: "wx", mode: 0o600 });
  if (values.action === "snapshot") {
    console.log(JSON.stringify({ batchDir, baselineCommit: before.baselineCommit,
      sourceSha256: before.sourceSha256, dirty: before.dirty, files: before.files.length }));
    return;
  }
  mkdirSync(join(batchDir, "tmp"));
  const env = validationEnvironment(process.env, batchDir);
  const rustTools = {};
  for (const tool of ["cargo", "rustc"]) {
    const version = spawnSync(tool, ["--version"], { cwd: root, encoding: "utf8", shell: false, env });
    if (version.error || version.status !== 0) throw new Error("Native Rust tool unavailable: " + tool);
    rustTools[tool] = version.stdout.trim();
  }
  const config = JSON.parse(readFileSync(join(root, "desktop/src-tauri/tauri.conf.json")));
  if (!["com.hugagent.desktop", "cn.hugagent.agent.desktop"].includes(config.identifier)) {
    throw new Error("Unknown product; validation cannot substitute its identity.");
  }
  const args = [actions[values.action], "--lib", "--locked", "--offline"];
  const log = openSync(join(batchDir, "cargo.log"), "wx", 0o600);
  let result;
  try {
    result = spawnSync("cargo", args, { cwd: join(root, "desktop/src-tauri"), env,
      shell: false, stdio: ["ignore", log, log] });
  } finally { closeSync(log); }
  const after = await captureSource(root);
  writeFileSync(join(batchDir, "source-after.json"), JSON.stringify(after, null, 2) + "\n", { flag: "wx", mode: 0o600 });
  const unchanged = before.sourceSha256 === after.sourceSha256 &&
    before.baselineCommit === after.baselineCommit && before.dirtyStatusSha256 === after.dirtyStatusSha256;
  const receipt = { schemaVersion: 1, issue: "WB-25", kind: "native-source-validation-only",
    action: values.action, rustTools, baselineCommit: before.baselineCommit, sourceSha256: before.sourceSha256,
    dirty: before.dirty, sourceUnchanged: unchanged, productIdentifier: config.identifier,
    updaterConfigSha256: createHash("sha256").update(JSON.stringify(config.plugins?.updater)).digest("hex"),
    command: ["cargo", ...args], targetDirectory: join(batchDir, "target"), architecture: process.arch,
    exitCode: result.status, status: !result.error && result.status === 0 && unchanged ? "pass" : "fail",
    packaging: "not-run", codeSigning: "not-run", updaterSignature: "not-run", installedAcceptance: "not-run",
    publication: "not-published", synchronization: "not-submitted" };
  writeFileSync(join(batchDir, "validation.json"), JSON.stringify(receipt, null, 2) + "\n", { flag: "wx", mode: 0o600 });
  console.log(JSON.stringify({ batchDir, ...receipt }));
  if (receipt.status !== "pass") throw new Error("Native source validation failed; preserve cargo.log and source receipts.");
}

if (process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))) {
  const { values } = parseArgs({ options: Object.fromEntries(
    ["checkout", "baseline-commit", "source-sha256", "action", "batch"].map(name => [name, { type: "string" }])) });
  runValidation(values).catch(error => { console.error(error.message); process.exitCode = 1; });
}
