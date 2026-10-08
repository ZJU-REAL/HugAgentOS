import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { isBrowserRuntimeCode, macRuntimeSigningTargets, refreshSignedBrowserManifest } from "./macos-runtime-signing.mjs";

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), "mac-browser-signing-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const file = (name) => { const path = join(root, name); mkdirSync(join(path, ".."), { recursive: true }); writeFileSync(path, "signed browser"); return path; };
  return { root, file };
}

test("framework helpers are signed before the framework executable and parent application", (t) => {
  const { root, file } = fixture(t);
  const framework = "native/browser/Chrome.app/Contents/Frameworks/Chrome.framework";
  const main = file(`${framework}/Versions/147/Chrome`);
  const shortcut = file(`${framework}/Versions/147/Helpers/web_app_shortcut_copier`);
  const helper = file(`${framework}/Versions/147/Helpers/Renderer.app/Contents/MacOS/Renderer`);
  const app = file("native/browser/Chrome.app/Contents/MacOS/Chrome");
  const { files, bundles } = macRuntimeSigningTargets(root);
  assert.ok(files.indexOf(helper) < files.indexOf(main));
  assert.ok(files.indexOf(shortcut) < files.indexOf(main));
  assert.ok(files.indexOf(main) < files.indexOf(app));
  assert.ok(bundles.indexOf(join(root, framework)) < bundles.indexOf(join(root, "native/browser/Chrome.app")));
  assert.ok(isBrowserRuntimeCode(root, helper));
  assert.ok(isBrowserRuntimeCode(root, join(root, "python/lib/python3.11/site-packages/playwright/driver/node")));
  assert.equal(isBrowserRuntimeCode(root, join(root, "python/bin/pandoc")), false);
});

test("the browser integrity pin follows delivered executable bytes after signing", (t) => {
  const { root, file } = fixture(t);
  const executable = "native/browser/Chrome"; file(executable);
  const manifest = join(root, "browser-runtime.json");
  writeFileSync(manifest, JSON.stringify({ executable, executable_sha256: "before signing" }));
  refreshSignedBrowserManifest(root);
  assert.equal(JSON.parse(readFileSync(manifest)).executable_sha256, createHash("sha256").update("signed browser").digest("hex"));
  const outside = fixture(t).file("outside");
  writeFileSync(manifest, JSON.stringify({ executable: outside }));
  assert.throws(() => refreshSignedBrowserManifest(root), /escapes the private runtime/);
});
