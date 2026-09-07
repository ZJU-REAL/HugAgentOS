import assert from "node:assert/strict";
import test from "node:test";

import { resolveHybridOnly } from "./desktop-hybrid-only.mjs";

test("默认构建保留三选一，不进仅混合模式", () => {
  assert.equal(resolveHybridOnly([], {}), false);
  assert.equal(resolveHybridOnly([], { JX_DESKTOP_HYBRID_ONLY: "0" }), false);
});

test("命令行开关与环境变量都能开启仅混合模式", () => {
  const env = { JX_DEFAULT_SERVER_BASE: "https://agent.example.test" };
  assert.equal(resolveHybridOnly(["--hybrid-only"], env), true);
  assert.equal(
    resolveHybridOnly([], { ...env, JX_DESKTOP_HYBRID_ONLY: "1" }),
    true,
  );
});

test("仅混合模式缺云端地址时硬失败，不打出指向开发默认地址的包", () => {
  assert.throws(
    () => resolveHybridOnly(["--hybrid-only"], {}),
    /JX_DEFAULT_SERVER_BASE/,
  );
  assert.throws(
    () => resolveHybridOnly(["--hybrid-only"], { JX_DEFAULT_SERVER_BASE: "  " }),
    /JX_DEFAULT_SERVER_BASE/,
  );
  assert.throws(
    () =>
      resolveHybridOnly(["--hybrid-only"], {
        JX_DEFAULT_SERVER_BASE: "agent.example.test",
      }),
    /http\(s\) 地址/,
  );
});
