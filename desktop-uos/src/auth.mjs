import { join } from "node:path";
import { readJson, writeJson } from "./storage.mjs";

export async function loadToken(configDir) {
  const auth = await readJson(join(configDir, "auth.json"), {});
  return typeof auth.token === "string" && auth.token ? auth.token : null;
}

export async function saveToken(configDir, token) {
  await writeJson(join(configDir, "auth.json"), { token: token || null });
}

export async function redeem(http, serverBase, ticket) {
  const response = await http.fetch(
    `${serverBase}/api/v1/auth/desktop/redeem`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ticket }),
    },
  );
  if (!response.ok) throw new Error(`换票失败: HTTP ${response.status}`);
  const body = await response.json();
  const token = body?.data?.token;
  if (!token) throw new Error("换票响应缺少 token");
  return token;
}

export async function validate(http, serverBase, cookieName, token) {
  try {
    const response = await http.fetch(`${serverBase}/api/v1/auth/session/check`, {
      headers: { cookie: `${cookieName}=${token}` },
    });
    return response.ok;
  } catch {
    return false;
  }
}
