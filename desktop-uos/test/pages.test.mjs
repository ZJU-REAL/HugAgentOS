import assert from "node:assert/strict";
import { Script } from "node:vm";
import test from "node:test";

import { initPage, setupPage } from "../src/pages.mjs";

function inlineScripts(html) {
  return [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((match) => match[1]);
}

test("generated desktop pages contain syntactically valid inline scripts", () => {
  for (const html of [initPage(), setupPage()]) {
    const scripts = inlineScripts(html);
    assert.ok(scripts.length > 0);
    for (const source of scripts) {
      assert.doesNotThrow(() => new Script(source));
    }
  }
});
