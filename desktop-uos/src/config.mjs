import { join } from "node:path";
import { normalizedBase, readJson, writeJson } from "./storage.mjs";

export const LOCAL_SERVER_BASE = "http://127.0.0.1:32101";
export const TARGET = "linux-aarch64";

export function defaultConfig() {
  return {
    deployment_mode: "remote",
    provision_mode: null,
    cloud_server_base: "",
    server_base: process.env.JX_DEFAULT_SERVER_BASE || "http://localhost:3000",
    cookie_name: "jx_session",
    insecure_tls: false,
  };
}

export function provisionMode(config) {
  return config.provision_mode ||
    (config.deployment_mode === "local" ? "local_only" : "cloud_only");
}

export function cloudBase(config) {
  return normalizedBase(
    config.cloud_server_base ||
      (config.deployment_mode === "remote" ? config.server_base : "") ||
      process.env.JX_DEFAULT_SERVER_BASE ||
      "http://localhost:3000",
  );
}

export async function loadConfig(configDir) {
  const saved = await readJson(join(configDir, "server.json"), {});
  const config = { ...defaultConfig(), ...saved };
  if (process.env.HUGAGENT_SERVER_BASE?.trim()) {
    config.server_base = process.env.HUGAGENT_SERVER_BASE;
    config.deployment_mode = "remote";
  }
  if (provisionMode(config) === "dual" && config.deployment_mode === "local") {
    config.deployment_mode = "remote";
    config.server_base = cloudBase(config);
  }
  config.server_base = normalizedBase(config.server_base);
  config.cookie_name = String(config.cookie_name || "jx_session");
  return config;
}

export async function saveConfig(configDir, config) {
  await writeJson(join(configDir, "server.json"), config);
}

export async function saveServerBase(configDir, base) {
  const config = await loadConfig(configDir);
  const normalized = normalizedBase(base);
  if (provisionMode(config) === "local_only") config.provision_mode = "cloud_only";
  config.deployment_mode = "remote";
  config.server_base = normalized;
  config.cloud_server_base = normalized;
  await saveConfig(configDir, config);
}

export async function provision(configDir, mode, cloud = "") {
  const config = await loadConfig(configDir);
  const cloudServer = cloud ? normalizedBase(cloud) : config.cloud_server_base;
  if (mode !== "local_only" && !cloudServer) {
    throw new Error("云端或双模式必须填写服务器地址");
  }
  config.provision_mode = mode;
  if (cloudServer) config.cloud_server_base = cloudServer;
  if (mode === "local_only") {
    config.deployment_mode = "local";
    config.server_base = LOCAL_SERVER_BASE;
  } else {
    config.deployment_mode = "remote";
    config.server_base = cloudServer;
  }
  await saveConfig(configDir, config);
}

export async function isProvisioned(configDir) {
  const config = await readJson(join(configDir, "server.json"), null);
  if (!config?.provision_mode) return false;
  return config.provision_mode === "local_only" || Boolean(config.cloud_server_base?.trim());
}

export function updateBase(config) {
  const override = process.env.HUGAGENT_UPDATE_SERVER_BASE?.trim();
  if (override) return normalizedBase(override);
  if (config.deployment_mode === "local") {
    return normalizedBase(
      process.env.JX_DESKTOP_UPDATE_BASE ||
        process.env.JX_DEFAULT_SERVER_BASE ||
        "http://localhost:3000",
    );
  }
  return config.server_base;
}
