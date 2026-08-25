import assert from "node:assert/strict";
import test from "node:test";

import { syncDesktopRuntime } from "../src/hybrid.mjs";


function response(data, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return { data }; },
  };
}


test("desktop model sync imports only capability gateway credentials", async () => {
  const requests = [];
  const http = {
    async fetch(url, options = {}) {
      requests.push({ url, options });
      if (url === "https://cloud.example/api/v1/desktop/capability/token") {
        return response({ token: "dcap1.short-lived", expires_in: 86400 });
      }
      if (url === "https://cloud.example/api/v1/desktop/capability/models") {
        assert.equal(options.headers.authorization, "Bearer dcap1.short-lived");
        return response({
          version: 1,
          providers: [{
            provider_id: "private-deepseek",
            display_name: "DeepSeek",
            provider_type: "chat",
            provider: "openai_compatible",
            model_name: "deepseek-private",
            extra_config: { context_length: 131072 },
            is_active: true,
          }],
          role_assignments: [{ role_key: "main_agent", provider_id: "private-deepseek" }],
        });
      }
      if (url.endsWith("/api/v1/desktop/capability/cloud-bridge")) return response({ ok: true });
      if (url === "http://127.0.0.1:32101/api/v1/models/import") {
        return response({ imported_providers: 1 });
      }
      throw new Error(`unexpected request: ${url}`);
    },
  };

  await syncDesktopRuntime({
    http,
    cloudBase: "https://cloud.example",
    cookieName: "session",
    token: "cloud-session-cookie",
    bridgeSecret: "local-bridge-secret",
  });

  assert.equal(requests.some(({ url }) => url.endsWith("/api/v1/models/export")), false);
  const imported = requests.find(({ url }) => url.endsWith("/api/v1/models/import"));
  const body = JSON.parse(imported.options.body);
  assert.equal(body.providers[0].api_key, "dcap1.short-lived");
  assert.equal(
    body.providers[0].base_url,
    "https://cloud.example/api/v1/desktop/capability/gateway/models/private-deepseek",
  );
  assert.equal(JSON.stringify(body).includes("192.0.2."), false);
  assert.equal(imported.options.headers.authorization, "Bearer local-bridge-secret");
});
