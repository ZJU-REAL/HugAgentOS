import { randomBytes } from "node:crypto";
import { chmod, mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { LOCAL_SERVER_BASE } from "./config.mjs";

export async function loadOrCreateBridgeSecret(configDir) {
  const path = join(configDir, "bridge.secret");
  try {
    const existing = (await readFile(path, "utf8")).trim();
    if (existing.length >= 32) return existing;
  } catch {}
  const secret = randomBytes(32).toString("hex");
  await mkdir(configDir, { recursive: true, mode: 0o700 });
  await writeFile(path, secret, { mode: 0o600 });
  await chmod(path, 0o600).catch(() => {});
  return secret;
}

async function cloudUser(http, base, cookieName, token) {
  const response = await http.fetch(`${base}/api/v1/me`, { headers: { cookie: `${cookieName}=${token}` } });
  if (!response.ok) throw new Error(`云端用户接口 HTTP ${response.status}`);
  const body = await response.json();
  const data = body.data || body;
  const id = data.user_center_id || data.user_id;
  if (!id) throw new Error("云端用户响应缺少 user_center_id");
  const url = new URL(base);
  return Buffer.from(JSON.stringify({
    user_center_id: `cloud:${url.host}:${id}`,
    username: data.username || id,
    email: data.email || null,
    avatar_url: data.avatar_url || null,
  })).toString("base64");
}

async function syncModels(http, base, cookieName, token, secret) {
  const exported = await http.fetch(`${base}/api/v1/models/export`, { headers: { cookie: `${cookieName}=${token}` } });
  if (!exported.ok) throw new Error(`模型导出 HTTP ${exported.status}`);
  const body = await exported.json();
  const imported = await http.fetch(`${LOCAL_SERVER_BASE}/api/v1/models/import`, {
    method: "POST",
    headers: { authorization: `Bearer ${secret}`, "content-type": "application/json" },
    body: JSON.stringify(body.data || {}),
  });
  if (!imported.ok) throw new Error(`模型导入 HTTP ${imported.status}`);
}

async function syncCapabilities(http, base, cookieName, token, secret) {
  const issued = await http.fetch(`${base}/api/v1/desktop/capability/token`, {
    method: "POST",
    headers: { cookie: `${cookieName}=${token}` },
  });
  if (!issued.ok) throw new Error(`能力令牌签发 HTTP ${issued.status}`);
  const data = (await issued.json()).data || {};
  if (!data.token) throw new Error("能力令牌响应缺少 token");
  const pushed = await http.fetch(`${LOCAL_SERVER_BASE}/api/v1/desktop/capability/cloud-bridge`, {
    method: "POST",
    headers: { authorization: `Bearer ${secret}`, "content-type": "application/json" },
    body: JSON.stringify({ cloud_base: base, token: data.token, expires_in: data.expires_in || 86400 }),
  });
  if (!pushed.ok) throw new Error(`能力桥配置 HTTP ${pushed.status}`);
}

async function retry(operation, name) {
  let last;
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    try { return await operation(); } catch (error) {
      last = error;
      console.warn(`[hybrid] ${name}失败（${attempt}/5）: ${error.message}`);
      if (attempt < 5) await new Promise((resolve) => setTimeout(resolve, attempt * 15_000));
    }
  }
  throw last;
}

export async function onCloudLogin({ http, cloudBase, cookieName, token, bridgeSecret, localServer, state }) {
  try {
    state.bridgeUser = await cloudUser(http, cloudBase, cookieName, token);
  } catch (error) {
    console.warn(`[hybrid] 云端身份桥接失败: ${error.message}`);
    return;
  }
  for (let attempt = 0; attempt < 480 && !await localServer.isReady(); attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 5_000));
  }
  if (!await localServer.isReady()) return;
  await Promise.allSettled([
    retry(() => syncModels(http, cloudBase, cookieName, token, bridgeSecret), "模型配置下发"),
    retry(() => syncCapabilities(http, cloudBase, cookieName, token, bridgeSecret), "能力桥下发"),
  ]);
  if (!state.capabilityTimer) {
    state.capabilityTimer = setInterval(() => {
      void syncCapabilities(http, cloudBase, cookieName, state.token || token, bridgeSecret)
        .catch((error) => console.warn(`[hybrid] 能力桥定期刷新失败: ${error.message}`));
    }, 4 * 60 * 60 * 1_000);
    state.capabilityTimer.unref();
  }
}
