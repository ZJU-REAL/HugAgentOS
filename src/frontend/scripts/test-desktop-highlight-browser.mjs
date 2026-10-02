import assert from "node:assert/strict";
import { build } from "esbuild";
import { chromium } from "playwright";
import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import { resolve, basename } from "node:path";

const output = resolve("node_modules/.tmp/desktop-highlight-browser");
await mkdir(output, { recursive: true });
await build({ entryPoints: ["src/utils/syntaxHighlight.ts"], bundle: true, splitting: true,
  format: "esm", outdir: output, platform: "browser" });
const server = createServer(async (req, res) => {
  try {
    if (req.url === "/") {
      res.setHeader("content-type", "text/html");
      res.end('<div id="target"></div><script type="module">import * as highlighter from "/syntaxHighlight.js";window.highlighter=highlighter;</script>');
    } else {
      res.setHeader("content-type", "text/javascript");
      res.end(await readFile(resolve(output, basename(req.url))));
    }
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(done => server.listen(0, "127.0.0.1", done));
let browser;
try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM });
  const page = await browser.newPage();
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.waitForFunction(() => !!window.highlighter);
  await page.evaluate(() => {
    const code = 'program example\nprint *, "<script>"\nend program example';
    const html = window.highlighter.highlightSyntax(code, "fortran");
    document.querySelector("#target").innerHTML = window.highlighter.lazyHighlightedHtml(
      `<pre><code class="hljs language-fortran">${html}</code></pre>`, "fortran");
  });
  await page.waitForSelector("code .hljs-keyword");
  assert.equal(await page.locator("code").textContent(), 'program example\nprint *, "<script>"\nend program example');
  assert.equal(await page.locator("#target script").count(), 0);
  console.log("Lazy uncommon grammar renders safely without a React subscriber.");
} finally {
  await browser?.close();
  await new Promise(done => server.close(done));
}
