import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";

const script = await readFile(new URL("../shared/chrome/menu.js", import.meta.url), "utf8");

function titlebar() {
  const listeners = {};
  const clicks = {};
  const item = { dataset: { act: "new_window" }, addEventListener: (event, fn) => { clicks[event] = fn; } };
  const bar = {
    getAttribute: () => "", setAttribute() {}, addEventListener() {},
    querySelector: () => null,
    querySelectorAll: (selector) => selector === "[data-act]" ? [item] : [],
  };
  const window = { location: { href: "" }, addEventListener() {}, innerWidth: 1280 };
  const document = {
    getElementById: () => bar, querySelector: () => null,
    documentElement: { dataset: {}, lang: "zh-CN", style: { setProperty() {} } },
    addEventListener: (event, fn) => { (listeners[event] ||= []).push(fn); },
  };
  vm.runInNewContext(script, {
    window, document, URLSearchParams, location: { search: "" },
    localStorage: { getItem: () => null }, navigator: { language: "zh-CN" },
    MutationObserver: class { observe() {} },
  });
  return { window, clicks, key(event) {
    for (const listener of listeners.keydown) listener({ preventDefault() {}, ...event });
  } };
}

test("Ctrl+Shift+N opens a new window while Ctrl+N retains New Chat", () => {
  const ui = titlebar();
  ui.key({ key: "N", ctrlKey: true, shiftKey: true });
  assert.equal(ui.window.location.href, "/__desktop/menu?action=new_window");
  ui.key({ key: "n", ctrlKey: true, shiftKey: false });
  assert.equal(ui.window.location.href, "/__desktop/menu?action=new_chat");
  ui.window.location.href = "";
  ui.key({ key: "n", ctrlKey: true, shiftKey: true, altKey: true });
  assert.equal(ui.window.location.href, "");
});

test("File New Window click uses the current webview navigation sentinel", () => {
  const ui = titlebar();
  ui.clicks.click({ stopPropagation() {} });
  assert.equal(ui.window.location.href, "/__desktop/menu?action=new_window");
});
