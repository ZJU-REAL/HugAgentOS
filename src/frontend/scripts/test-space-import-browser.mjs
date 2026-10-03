import assert from "node:assert/strict";
import { build } from "esbuild";
import { mkdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createServer } from "node:http";
import { chromium } from "playwright";

const output = resolve("node_modules/.tmp/space-import-browser");
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
import React from "react";
import { createRoot } from "react-dom/client";
import { MySpaceImportModal } from "./src/components/file/MySpaceImportModal";
import "antd/dist/reset.css";
createRoot(document.getElementById("root")).render(<MySpaceImportModal open onClose={() => {}} />);
`, resolveDir: process.cwd(), loader: "tsx" }, outfile: resolve(output, "fixture.js"),
  bundle: true, format: "esm", jsx: "automatic", define: { "import.meta.env": "{\"VITE_DEFAULT_LANGUAGE\":\"zh-CN\"}" },
  loader: { ".svg": "dataurl", ".png": "dataurl", ".md": "text", ".woff": "dataurl", ".woff2": "dataurl", ".ttf": "dataurl" },
  external: ["/loader.gif", "/loader-done.png"] });
const server = createServer(async (req, res) => {
  if (req.url === "/fixture.js" || req.url === "/fixture.css") {
    res.setHeader("Content-Type", req.url.endsWith("js") ? "text/javascript" : "text/css");
    res.end(await readFile(resolve(output, req.url.slice(1))));
  } else res.end(`<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>`);
});
await new Promise(r => server.listen(0, "127.0.0.1", r));
let browser;
try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH });
  const page = await browser.newPage();
  let fail = true;
  let race = false;
  let releaseDocument;
  let startDocument;
  let finishDocument;
  const documentStarted = new Promise(resolve => { startDocument = resolve; });
  const documentFinished = new Promise(resolve => { finishDocument = resolve; });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/api/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/v1/artifacts")) {
      if (fail) return route.fulfill({ status: 409, json: { detail: "空间文件同步未完成" } });
      if (race && url.searchParams.get("type") === "document" && url.searchParams.get("page_size") === "20") {
        startDocument();
        await new Promise(resolve => { releaseDocument = resolve; });
        await route.fulfill({ json: { code: 200, data: { items: [{ id: "stale", name: "过时.docx", type: "document" }], total: 1, has_more: false } } });
        finishDocument();
        return;
      }
      if (race && url.searchParams.get("type") === "image") {
        return route.fulfill({ json: { code: 200, data: { items: [{ id: "current", name: "当前.png", type: "image" }], total: 1, has_more: false } } });
      }
      return route.fulfill({ json: { code: 200, data: { items: [{ id: "file", file_id: "file", name: "报告.docx", type: "document", mime_type: "application/octet-stream", created_at: "2026-01-01" }], total: 1, has_more: false } } });
    }
    return route.fulfill({ json: { code: 200, data: { items: [], tree: [], total: 0, has_more: false } } });
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.getByRole("alert").filter({ hasText: "空间文件同步未完成" }).waitFor();
  assert.equal(await page.getByText("暂无文件", { exact: true }).count(), 0);
  fail = false;
  await page.getByRole("button", { name: /重\s*试/ }).click();
  await page.getByText("报告.docx", { exact: true }).waitFor();
  assert.equal(await page.getByRole("alert").count(), 0);
  race = true;
  await page.getByRole("tab", { name: /文档/ }).click();
  await documentStarted;
  await page.getByRole("tab", { name: /图片/ }).click();
  await page.getByText("当前.png", { exact: true }).waitFor();
  releaseDocument();
  await documentFinished;
  await page.waitForTimeout(100);
  assert.equal(await page.getByText("过时.docx", { exact: true }).count(), 0);
  assert.equal(await page.getByText("当前.png", { exact: true }).count(), 1);
  assert.deepEqual(errors, []);
  console.log("Space import: error is visible, retry restores files, no false empty state.");
} finally {
  await browser?.close();
  await new Promise(r => server.close(r));
}
