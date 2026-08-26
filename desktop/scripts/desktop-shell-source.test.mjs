import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const desktopDir = join(dirname(fileURLToPath(import.meta.url)), "..");
const rustDir = join(desktopDir, "src-tauri", "src");

test("desktop webviews keep DPI handling consistent across the shell", () => {
  const libSource = readFileSync(join(rustDir, "lib.rs"), "utf8");
  const updateSource = readFileSync(join(rustDir, "update.rs"), "utf8");

  assert.match(libSource, /WEBVIEW_BROWSER_ARGS/);
  assert.match(libSource, /apply_display_zoom/);
  assert.match(updateSource, /WEBVIEW_BROWSER_ARGS/);
  assert.match(updateSource, /apply_display_zoom/);
});

test("the SPA receives the current injected platform titlebar", () => {
  const proxySource = readFileSync(join(rustDir, "proxy.rs"), "utf8");

  assert.match(proxySource, /platform_titlebar_block/);
  assert.match(proxySource, /TB_MENU/);
});
