import { chmod, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

export async function readJson(path, fallback = null) {
  try {
    return JSON.parse(await readFile(path, "utf8"));
  } catch {
    return fallback;
  }
}

export async function atomicWrite(path, contents, mode = 0o600) {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const next = join(dirname(path), `.${Date.now()}-${process.pid}.next`);
  await writeFile(next, contents, { encoding: "utf8", mode });
  await chmod(next, mode).catch(() => {});
  await rename(next, path);
}

export async function writeJson(path, value, mode = 0o600) {
  await atomicWrite(path, `${JSON.stringify(value, null, 2)}\n`, mode);
}

export function normalizedBase(value) {
  const raw = String(value ?? "").trim().replace(/\/+$/, "");
  if (!raw) return "";
  const parsed = new URL(raw);
  if (!new Set(["http:", "https:"]).has(parsed.protocol)) {
    throw new Error("服务器地址只支持 http 或 https");
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error("服务器地址不能包含账号、查询参数或锚点");
  }
  return parsed.toString().replace(/\/$/, "");
}
