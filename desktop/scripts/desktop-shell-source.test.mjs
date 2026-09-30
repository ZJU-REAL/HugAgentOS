import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const desktopDir = join(dirname(fileURLToPath(import.meta.url)), "..");
const rustDir = join(desktopDir, "src-tauri", "src");

test("desktop webviews keep zoom handling consistent across the shell", () => {
  const libSource = readFileSync(join(rustDir, "lib.rs"), "utf8");

  assert.match(libSource, /ZOOM_STEPS/);
  assert.match(libSource, /apply_user_zoom/);
  assert.match(libSource, /restore_zoom_on_load/);
});

test("the SPA receives the current injected platform titlebar", () => {
  const proxySource = readFileSync(join(rustDir, "proxy.rs"), "utf8");

  assert.match(proxySource, /platform_titlebar_block/);
  const chromeSource = readFileSync(join(rustDir, "window_chrome.rs"), "utf8");
  assert.match(chromeSource, /TB_MENU/);
});
