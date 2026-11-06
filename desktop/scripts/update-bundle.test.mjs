import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  slimArtifactName,
  updateBundleConfig,
  withParkedFullBundle,
  writeUpdateBundleInputs,
} from "./update-bundle.mjs";

function scratch(t) {
  const root = mkdtempSync(join(tmpdir(), "update-bundle-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return root;
}

test("slim artifacts are renamed so publishing requires their runtime", () => {
  assert.equal(slimArtifactName("HugAgentOS_1.0.6_x64-setup.exe"), "HugAgentOS_1.0.6_x64-update-setup.exe");
  assert.equal(slimArtifactName("HugAgentOS_1.0.6_x64-setup.exe.sig"), "HugAgentOS_1.0.6_x64-update-setup.exe.sig");
  assert.equal(slimArtifactName("HugAgentOS.app.tar.gz"), "HugAgentOS-update.app.tar.gz");
  assert.equal(slimArtifactName("HugAgentOS_1.0.6_amd64.AppImage.sig"), "HugAgentOS_1.0.6_amd64-update.AppImage.sig");
  assert.equal(slimArtifactName("HugAgentOS.app"), null);
  // publish-desktop.py recognises slim packages with this pattern.
  for (const name of ["a-update-setup.exe", "a-update.app.tar.gz", "a-update.AppImage"]) {
    assert.match(name, /-update[-_.]/);
  }
});

test("the update overlay swaps only the runtime archive for a placeholder", () => {
  const config = updateBundleConfig("darwin");
  assert.deepEqual(config.bundle.targets, ["app"]);
  assert.deepEqual(config.bundle.resources, {
    "../generated/runtime-core.tar.gz": null,
    "../generated/update/runtime-core.tar.gz": "runtime-core.tar.gz",
  });
  assert.deepEqual(updateBundleConfig("win32").bundle.targets, ["nsis"]);
  assert.deepEqual(updateBundleConfig("linux").bundle.targets, ["appimage"]);
  assert.throws(() => updateBundleConfig("freebsd"));
});

test("update inputs need the full build and leave its runtime cache intact", (t) => {
  const root = scratch(t);
  assert.throws(() => writeUpdateBundleInputs(root, "win32"), /run the full build/);
  mkdirSync(join(root, "generated"));
  for (const name of ["runtime-core.tar.gz", "runtime-manifest.json", "server-ce.zip"]) {
    writeFileSync(join(root, "generated", name), "full");
  }
  const config = writeUpdateBundleInputs(root, "win32");
  assert.equal(readFileSync(join(root, "generated", "update", "runtime-core.tar.gz"), "utf8"), "");
  assert.equal(readFileSync(join(root, "generated", "runtime-core.tar.gz"), "utf8"), "full");
  assert.deepEqual(JSON.parse(readFileSync(config, "utf8")).bundle.targets, ["nsis"]);
});

test("the full bundle survives the update build and slim artifacts land beside it", (t) => {
  const release = scratch(t);
  mkdirSync(join(release, "bundle", "nsis"), { recursive: true });
  writeFileSync(join(release, "bundle", "nsis", "App_1.0.6_x64-setup.exe"), "full");
  const output = withParkedFullBundle(release, () => {
    mkdirSync(join(release, "bundle", "nsis"), { recursive: true });
    writeFileSync(join(release, "bundle", "nsis", "App_1.0.6_x64-setup.exe"), "slim");
    writeFileSync(join(release, "bundle", "nsis", "App_1.0.6_x64-setup.exe.sig"), "sig");
  });
  assert.equal(readFileSync(join(release, "bundle", "nsis", "App_1.0.6_x64-setup.exe"), "utf8"), "full");
  assert.deepEqual(readdirSync(join(output, "nsis")).sort(),
    ["App_1.0.6_x64-update-setup.exe", "App_1.0.6_x64-update-setup.exe.sig"]);
  assert.equal(existsSync(join(release, "bundle-full")), false);
});

test("a failed update build restores the full bundle", (t) => {
  const release = scratch(t);
  mkdirSync(join(release, "bundle", "nsis"), { recursive: true });
  writeFileSync(join(release, "bundle", "nsis", "App-setup.exe"), "full");
  assert.throws(() => withParkedFullBundle(release, () => {
    mkdirSync(join(release, "bundle"));
    throw new Error("build failed");
  }), /build failed/);
  assert.equal(readFileSync(join(release, "bundle", "nsis", "App-setup.exe"), "utf8"), "full");
});
