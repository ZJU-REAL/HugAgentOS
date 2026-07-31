import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
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
  const source = readFileSync(join(rustDir, "lib.rs"), "utf8");
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
  assert.match(smoke, /import_module\("cli"\)/);
});

test("macOS release falls back to ad-hoc signing without Apple credentials", () => {
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

  assert.match(builder, /APPLE_SIGNING_IDENTITY\?\.trim\(\) \|\| "-"/);
  assert.doesNotMatch(builder, /APPLE_SIGNING_IDENTITY is required/);
  assert.match(builder, /\["--force", "--sign", identity\]/);
  assert.match(
    workflow,
    /APPLE_SIGNING_IDENTITY: \$\{\{ secrets\.APPLE_SIGNING_IDENTITY \|\| '-' \}\}/,
  );
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
