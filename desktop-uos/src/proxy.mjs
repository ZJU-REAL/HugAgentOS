import { createReadStream } from "node:fs";
import { access, readFile, stat } from "node:fs/promises";
import http from "node:http";
import https from "node:https";
import { extname, join, normalize, relative, resolve } from "node:path";
import { loginPage, initPage, serverConfigPage, setupPage } from "./pages.mjs";

const PROXY_PREFIXES = ["/api", "/files", "/site", "/docs/"];
const HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);
const MIME = {
  ".css": "text/css; charset=utf-8",
  ".gif": "image/gif",
  ".html": "text/html; charset=utf-8",
  ".ico": "image/x-icon",
  ".jpeg": "image/jpeg",
  ".jpg": "image/jpeg",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function send(res, status, type, body) {
  const value = Buffer.from(body);
  res.writeHead(status, {
    "content-type": type,
    "content-length": value.length,
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  res.end(value);
}

function sendJson(res, value, status = 200) {
  send(res, status, "application/json; charset=utf-8", JSON.stringify(value));
}

function isProxyPath(pathname) {
  return PROXY_PREFIXES.some((prefix) =>
    prefix.endsWith("/") ? pathname.startsWith(prefix) : pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
}

function safeWebPath(webDir, pathname) {
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return null;
  }
  const candidate = resolve(webDir, `.${normalize(decoded)}`);
  const rel = relative(resolve(webDir), candidate);
  return rel.startsWith("..") || rel.includes(`..${process.platform === "win32" ? "\\" : "/"}`)
    ? null
    : candidate;
}

async function fileExists(path) {
  try {
    return (await stat(path)).isFile();
  } catch {
    return false;
  }
}

async function serveFile(res, path, cache = true) {
  const info = await stat(path);
  res.writeHead(200, {
    "content-type": MIME[extname(path).toLowerCase()] || "application/octet-stream",
    "content-length": info.size,
    "cache-control": cache ? "public,max-age=31536000,immutable" : "no-cache",
    "x-content-type-options": "nosniff",
  });
  createReadStream(path).pipe(res);
}

function sanitizedHeaders(headers) {
  const output = {};
  for (const [name, value] of Object.entries(headers)) {
    const lower = name.toLowerCase();
    if (
      HOP_HEADERS.has(lower) ||
      ["host", "cookie", "accept-encoding", "x-hugagent-target", "x-desktop-bridge", "x-desktop-bridge-user"].includes(lower)
    ) continue;
    if (value !== undefined) output[lower] = value;
  }
  output["accept-encoding"] = "identity";
  return output;
}

function pipeResponse(upstream, res) {
  const headers = {};
  for (const [name, value] of Object.entries(upstream.headers)) {
    if (!HOP_HEADERS.has(name.toLowerCase()) && value !== undefined) headers[name] = value;
  }
  headers["x-content-type-options"] ||= "nosniff";
  res.writeHead(upstream.statusCode || 502, headers);
  upstream.pipe(res);
}

function makeUpstream(req, target, state, toLocal, onResponse) {
  const parsed = new URL(target);
  const transport = parsed.protocol === "https:" ? https : http;
  const headers = sanitizedHeaders(req.headers);
  if (toLocal) {
    headers["x-desktop-bridge"] = state.bridgeSecret;
    if (state.bridgeUser) headers["x-desktop-bridge-user"] = state.bridgeUser;
  } else if (state.token) {
    headers.cookie = `${state.cookieName}=${state.token}`;
  }
  const upstream = transport.request(
    parsed,
    {
      method: req.method,
      headers,
      agent: state.http.agentFor(parsed),
      timeout: 120_000,
    },
    onResponse,
  );
  upstream.on("timeout", () => upstream.destroy(new Error("上游请求超时")));
  return upstream;
}

function proxyRequest(req, res, requestUrl, state) {
  const headerTarget = String(req.headers["x-hugagent-target"] || "").toLowerCase();
  const queryTarget = requestUrl.searchParams.get("hg_target");
  const toLocal = state.hybridLocal && (headerTarget === "local" || queryTarget === "local");
  const base = toLocal ? state.localBase : state.serverBase;
  const target = `${base}${requestUrl.pathname}${requestUrl.search}`;
  const upstream = makeUpstream(req, target, state, toLocal, (response) => {
    const canFallback =
      state.hybridLocal &&
      !toLocal &&
      req.method === "GET" &&
      (requestUrl.pathname === "/site" || requestUrl.pathname.startsWith("/site/")) &&
      response.statusCode === 404;
    if (!canFallback) return pipeResponse(response, res);
    response.resume();
    const localTarget = `${state.localBase}${requestUrl.pathname}${requestUrl.search}`;
    const retry = makeUpstream(req, localTarget, state, true, (localResponse) =>
      pipeResponse(localResponse, res),
    );
    retry.on("error", (error) => {
      if (!res.headersSent) send(res, 502, "text/plain; charset=utf-8", `代理本机服务失败: ${error.message}`);
      else res.destroy(error);
    });
    retry.end();
  });
  upstream.on("error", (error) => {
    if (!res.headersSent) send(res, 502, "text/plain; charset=utf-8", `代理上游失败: ${error.message}`);
    else res.destroy(error);
  });
  req.pipe(upstream);
}

export async function startProxy({ webDir, getState, localServer }) {
  await access(join(webDir, "index.html"));
  let origin = "";
  const server = http.createServer(async (req, res) => {
    try {
      if (!req.url) return send(res, 400, "text/plain", "Bad Request");
      const requestUrl = new URL(req.url, origin || "http://127.0.0.1");
      const host = String(req.headers.host || "");
      if (origin && host !== new URL(origin).host) return send(res, 421, "text/plain", "Misdirected Request");
      const requestOrigin = req.headers.origin;
      if (requestOrigin && requestOrigin !== origin) return send(res, 403, "text/plain", "Forbidden Origin");
      const state = await getState();

      if (requestUrl.pathname === "/__desktop/login") {
        return send(res, 200, "text/html; charset=utf-8", loginPage({ waiting: requestUrl.searchParams.get("waiting") === "1" }));
      }
      if (requestUrl.pathname === "/__desktop/init") {
        return send(res, 200, "text/html; charset=utf-8", initPage({
          cloudBase: state.cloudServerBase,
          mode: state.initMode,
          localSupported: state.localSupported,
        }));
      }
      if (requestUrl.pathname === "/__desktop/server-config") {
        return send(res, 200, "text/html; charset=utf-8", serverConfigPage(state.serverBase));
      }
      if (requestUrl.pathname === "/__desktop/setup") {
        return send(res, 200, "text/html; charset=utf-8", setupPage({ localSupported: state.localSupported }));
      }
      if (requestUrl.pathname === "/__desktop/setup/status") {
        const status = await localServer.snapshot();
        return sendJson(res, {
          ...status,
          active_local: state.activeLocal,
          current_server_base: state.serverBase,
          local_server_base: state.localBase,
          provision_mode: state.provisionMode,
          continue_url: await state.continueUrl(),
        });
      }
      if (requestUrl.pathname === "/__desktop/setup/install" && req.method === "POST") {
        localServer.prepare();
        return sendJson(res, await localServer.snapshot(), 202);
      }
      if (isProxyPath(requestUrl.pathname)) return proxyRequest(req, res, requestUrl, state);

      const candidate = safeWebPath(webDir, requestUrl.pathname);
      if (candidate && await fileExists(candidate)) return serveFile(res, candidate);
      return serveFile(res, join(webDir, "index.html"), false);
    } catch (error) {
      if (!res.headersSent) send(res, 500, "text/plain; charset=utf-8", `桌面反代错误: ${error.message}`);
      else res.destroy(error);
    }
  });
  server.keepAliveTimeout = 65_000;
  await new Promise((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolveListen);
  });
  const address = server.address();
  origin = `http://127.0.0.1:${address.port}`;
  return { origin, close: () => new Promise((resolveClose) => server.close(resolveClose)) };
}

export { isProxyPath, safeWebPath, sanitizedHeaders };
