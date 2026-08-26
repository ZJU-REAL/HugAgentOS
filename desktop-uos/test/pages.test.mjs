import assert from "node:assert/strict";
import { Script } from "node:vm";
import test from "node:test";

import { initPage, setupPage } from "../src/pages.mjs";

function inlineScripts(html) {
  return [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((match) => match[1]);
}

test("generated desktop pages contain syntactically valid inline scripts", () => {
  for (const html of [
    initPage(),
    initPage({ brand: "HugAgentOS", cloudBase: "https://cloud.example.test", fixedDual: true, mode: "dual" }),
    setupPage({ brand: "HugAgentOS", dual: true }),
  ]) {
    const scripts = inlineScripts(html);
    assert.ok(scripts.length > 0);
    for (const source of scripts) {
      assert.doesNotThrow(() => new Script(source));
    }
  }
});

test("fixed dual initialization matches the one-action HugAgentOS desktop flow", () => {
  const html = initPage({
    brand: "HugAgentOS",
    cloudBase: "https://cloud.example.test",
    fixedDual: true,
    mode: "dual",
  });
  assert.match(html, /初始化 HugAgentOS/);
  assert.match(html, /name=provision&mode=dual/);
  assert.match(html, /id="cloudBase"[^>]+https:\/\/cloud\.example\.test/);
  assert.doesNotMatch(html, /<select/);
});

test("setup presents animated progress without exposing raw installation logs", () => {
  const html = setupPage({ brand: "HugAgentOS", dual: true });
  for (const animation of ["@keyframes spin", "@keyframes float", "@keyframes halo", "@keyframes sweep"]) {
    assert.match(html, new RegExp(animation));
  }
  assert.match(html, /role="progressbar"/);
  assert.doesNotMatch(html, /<pre\b|id="logs"|status\.logs/);
});
