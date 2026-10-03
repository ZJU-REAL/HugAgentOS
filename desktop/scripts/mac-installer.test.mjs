import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  explicitTauriTarget,
  validateDesktopBuildTarget,
} from "./desktop-build-target.mjs";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoDir = resolve(desktopDir, "..");
const rustDir = join(desktopDir, "src-tauri", "src");

test("desktop local install is offline and shared by all three operating systems", () => {
  const source = readFileSync(join(rustDir, "local_server.rs"), "utf8");
  const payload = readFileSync(join(rustDir, "local_payload.rs"), "utf8");
  for (const obsoleteNetworkStep of [
    "winget",
    "uv python install",
    "uv pip sync",
    "pip install",
    "curl ",
  ]) {
    assert.doesNotMatch(source, new RegExp(obsoleteNetworkStep, "i"));
    assert.doesNotMatch(payload, new RegExp(obsoleteNetworkStep, "i"));
  }
  assert.match(payload, /verify_file_hash/);
  assert.match(payload, /run_smoke_test/);
  assert.match(payload, /restore_previous/);
});

test("Windows macOS and Linux packages embed both source and runtime payloads", () => {
  for (const platform of ["windows", "macos", "linux"]) {
    const config = readFileSync(
      join(desktopDir, "src-tauri", `tauri.${platform}.conf.json`),
      "utf8",
    );
    for (const resource of [
      "server-ce.zip",
      "server-ce-manifest.json",
      "runtime-core.tar.gz",
      "runtime-manifest.json",
    ]) {
      assert.match(config, new RegExp(resource.replaceAll(".", "\\.")));
    }
  }
});

test("minimize-to-tray destroys the close confirmation window", () => {
  const source = readFileSync(join(rustDir, "dialogs.rs"), "utf8");
  const decisionStart = source.indexOf(
    'if let Some(cw) = app2.get_webview_window("close-confirm")',
  );
  assert.notEqual(decisionStart, -1);
  const decision = source.slice(decisionStart, source.indexOf("if exit", decisionStart));
  assert.match(decision, /cw\.hide\(\)/);
  assert.match(decision, /cw\.close\(\)/);
  assert.ok(decision.indexOf("cw.hide()") < decision.indexOf("cw.close()"));
});

test("CE project creation follows the provisioned desktop mode", () => {
  const deploymentStore = readFileSync(
    join(repoDir, "src", "frontend", "src", "stores", "deploymentModeStore.ts"),
    "utf8",
  );
  const projectComponent = (name) => {
    const overlayPath = join(
      repoDir,
      "ce",
      "overlay",
      "src",
      "frontend",
      "src",
      "components",
      "projects",
      name,
    );
    return existsSync(overlayPath)
      ? overlayPath
      : join(repoDir, "src", "frontend", "src", "components", "projects", name);
  };
  const projectsPanel = readFileSync(projectComponent("ProjectsPanel.tsx"), "utf8");
  const createModal = readFileSync(projectComponent("CreateProjectModal.tsx"), "utf8");

  assert.match(deploymentStore, /local_only'.*cloud: false, local: true/);
  assert.match(deploymentStore, /dual'.*cloud: true, local: true/);
  assert.match(projectsPanel, /projectCreationTargets\(isDesktop, provisionMode\)/);
  assert.match(projectsPanel, /canCreateCloudProject && canCreateLocalProject/);
  assert.match(projectsPanel, /\/__desktop\/pick-local-folder/);
  assert.match(createModal, /if \(!canCreateCloudProject\)/);
  assert.match(createModal, /if \(!canCreateCloudProject\) return null/);
});

test("release builder validates dependencies and relocatable runtime before archiving", () => {
  const builder = readFileSync(join(desktopDir, "scripts", "build-runtime.mjs"), "utf8");
  const smoke = readFileSync(join(desktopDir, "scripts", "runtime-smoke.py"), "utf8");
  assert.match(builder, /--only-binary/);
  assert.match(builder, /"pip", "check"/);
  assert.match(builder, /runtime-smoke\.py/);
  assert.match(builder, /signMacRuntime/);
  assert.match(builder, /Resuming validated/);
  assert.match(smoke, /from api\.app import app/);
});

test("macOS release requires configured signing credentials without ad-hoc fallback", () => {
  const builder = readFileSync(join(desktopDir, "scripts", "build-runtime.mjs"), "utf8");
  const overlayWorkflow = join(
    repoDir,
    "ce",
    "overlay",
    ".github",
    "workflows",
    "desktop-release.yml",
  );
  const workflow = readFileSync(
    existsSync(overlayWorkflow)
      ? overlayWorkflow
      : join(repoDir, ".github", "workflows", "desktop-release.yml"),
    "utf8",
  );

  assert.match(builder, /assertMacosSigning\(\)/);
  assert.doesNotMatch(builder, /APPLE_SIGNING_IDENTITY\?\.trim\(\) \|\| "-"/);
  assert.match(builder, /\["--force", "--sign", identity\]/);

  const configureStepStart = workflow.indexOf("- name: Configure macOS signing");
  const buildStepStart = workflow.indexOf("- name: Build and publish release");
  assert.notEqual(configureStepStart, -1);
  assert.ok(buildStepStart > configureStepStart);

  const configureStep = workflow.slice(configureStepStart, buildStepStart);
  const buildStep = workflow.slice(buildStepStart);
  assert.match(configureStep, /APPLE_CERTIFICATE_SECRET/);
  assert.doesNotMatch(configureStep, /echo 'APPLE_SIGNING_IDENTITY=-'/);
  assert.match(configureStep, /exit 1/);
  assert.match(configureStep, /vars\.MACOS_SIGNING_MODE/);
  assert.match(configureStep, /HUGAGENT_MACOS_SIGNING_MODE/);
  assert.match(configureStep, /self-signed/);
  assert.match(configureStep, /\[A-Fa-f0-9\]\{40\}/);
  assert.doesNotMatch(buildStep, /APPLE_CERTIFICATE:/);
  assert.doesNotMatch(buildStep, /APPLE_CERTIFICATE_PASSWORD:/);
});

test("CE generation uses an isolated pinned release-builder dependency", () => {
  const prepare = readFileSync(join(desktopDir, "scripts", "prepare-bundle.mjs"), "utf8");
  const requirements = readFileSync(
    join(desktopDir, "requirements-desktop-build.txt"),
    "utf8",
  );
  assert.match(prepare, /--with-requirements/);
  assert.match(prepare, /requirements-desktop-build\.txt/);
  assert.match(prepare, /TAURI_ENV_TARGET_TRIPLE/);
  assert.match(requirements, /^PyYAML==\d+\.\d+\.\d+$/m);
});

test("runtime archive creation opts into Windows extended-length paths", () => {
  const archiveBuilder = readFileSync(
    join(desktopDir, "scripts", "create-runtime-archive.py"),
    "utf8",
  );
  assert.match(archiveBuilder, /def _long_path/);
  assert.match(archiveBuilder, /\\\\\\\\\?\\\\UNC/);
  assert.match(archiveBuilder, /source = _long_path/);
  assert.match(archiveBuilder, /output = _long_path/);
});

test("offline desktop builds reject universal and cross-architecture targets", () => {
  assert.equal(explicitTauriTarget(["--target=x86_64-apple-darwin"]), "x86_64-apple-darwin");
  assert.equal(
    validateDesktopBuildTarget([], "darwin", "arm64"),
    "aarch64-apple-darwin",
  );
  assert.throws(
    () =>
      validateDesktopBuildTarget(
        ["--target", "universal-apple-darwin"],
        "darwin",
        "arm64",
      ),
    /separate installer/,
  );
  assert.throws(
    () =>
      validateDesktopBuildTarget(
        ["--target", "x86_64-apple-darwin"],
        "darwin",
        "arm64",
      ),
    /matching OS and CPU architecture/,
  );
});

test("CI config supports pinned self-signing without notarization and rejects incomplete modes",
  { skip: process.platform === "win32" }, () => {
    const overlay = join(repoDir, "ce/overlay/.github/workflows/desktop-release.yml");
    const workflow = readFileSync(existsSync(overlay) ? overlay : join(repoDir, ".github/workflows/desktop-release.yml"), "utf8");
    const step = workflow.split("- name: Configure macOS signing")[1].split("- name: Import macOS signing identity")[0];
    const script = step.split("run: |\n")[1].split("\n").map((line) => line.slice(10)).join("\n");
    const directory = mkdtempSync(join(tmpdir(), "mac-signing-ci-"));
    const pin = "0123456789ABCDEF0123456789ABCDEF01234567";
    const env = {
      PATH: process.env.PATH, GITHUB_ENV: join(directory, "env"),
      APPLE_CERTIFICATE_SECRET: "fixture-p12", APPLE_CERTIFICATE_PASSWORD_SECRET: "fixture-password",
      APPLE_SIGNING_IDENTITY_SECRET: pin, MACOS_SIGNING_MODE: "self-signed",
    };
    try {
      const run = (changes = {}) => spawnSync("bash", ["-e", "-c", script], { env: { ...env, ...changes }, encoding: "utf8" });
      const result = run();
      assert.equal(result.status, 0, result.stderr);
      const exported = readFileSync(env.GITHUB_ENV, "utf8");
      assert.match(exported, /HUGAGENT_MACOS_SIGNING_MODE=self-signed/);
      assert.match(exported, new RegExp("APPLE_SIGNING_IDENTITY=" + pin));
      assert.doesNotMatch(exported, /APPLE_TEAM_ID|APPLE_PASSWORD|APPLE_CERTIFICATE/);
      assert.notEqual(run({ APPLE_SIGNING_IDENTITY_SECRET: "-" }).status, 0);
      assert.notEqual(run({ MACOS_SIGNING_MODE: "developer-id" }).status, 0);
      assert.notEqual(run({ MACOS_SIGNING_MODE: "typo" }).status, 0);
      assert.notEqual(run({ APPLE_CERTIFICATE_SECRET: "" }).status, 0);
      assert.equal(run({
        MACOS_SIGNING_MODE: "developer-id",
        APPLE_SIGNING_IDENTITY_SECRET: "Developer ID Application: Example (ABCDE12345)",
        APPLE_ID_SECRET: "example@example.invalid", APPLE_PASSWORD_SECRET: "fixture",
        APPLE_TEAM_ID_SECRET: "ABCDE12345",
      }).status, 0);
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });
