import http from "node:http";
import https from "node:https";

class BufferedResponse {
  constructor(status, headers, body) {
    this.status = status;
    this.ok = status >= 200 && status < 300;
    this.headers = headers;
    this.body = body;
  }
  async json() {
    return JSON.parse(this.body.toString("utf8"));
  }
  async text() {
    return this.body.toString("utf8");
  }
  async arrayBuffer() {
    return this.body.buffer.slice(
      this.body.byteOffset,
      this.body.byteOffset + this.body.byteLength,
    );
  }
}

export function createHttpClient({ insecureTls = false } = {}) {
  const httpAgent = new http.Agent({ keepAlive: true });
  const httpsAgent = new https.Agent({
    keepAlive: true,
    rejectUnauthorized: !insecureTls,
  });

  return {
    agentFor(url) {
      return new URL(url).protocol === "https:" ? httpsAgent : httpAgent;
    },
    async fetch(url, options = {}) {
      const parsed = new URL(url);
      const transport = parsed.protocol === "https:" ? https : http;
      const body = options.body == null ? null : Buffer.from(options.body);
      return await new Promise((resolve, reject) => {
        const request = transport.request(
          parsed,
          {
            method: options.method || "GET",
            headers: {
              ...(options.headers || {}),
              ...(body ? { "content-length": body.length } : {}),
            },
            agent: parsed.protocol === "https:" ? httpsAgent : httpAgent,
            timeout: options.timeout ?? 20_000,
          },
          (response) => {
            const chunks = [];
            response.on("data", (chunk) => chunks.push(chunk));
            response.on("end", () => {
              resolve(
                new BufferedResponse(
                  response.statusCode || 0,
                  response.headers,
                  Buffer.concat(chunks),
                ),
              );
            });
          },
        );
        request.on("timeout", () => request.destroy(new Error("请求超时")));
        request.on("error", reject);
        if (body) request.end(body);
        else request.end();
      });
    },
    destroy() {
      httpAgent.destroy();
      httpsAgent.destroy();
    },
  };
}
