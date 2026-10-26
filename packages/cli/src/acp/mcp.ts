import {RequestError, type McpServer} from '@agentclientprotocol/sdk';
import {isAbsolute} from 'node:path';

export function mcpConfig(servers: McpServer[]): Record<string, unknown> {
  if (servers.length > 31) throw RequestError.invalidParams(undefined, 'Too many MCP servers');
  const result: Record<string, unknown> = Object.create(null);
  for (const server of servers) {
    if (!/^[a-zA-Z0-9_-]{1,64}$/.test(server.name) || server.name === 'acp_client' || Object.hasOwn(result,server.name))
      throw RequestError.invalidParams(undefined, 'Invalid or duplicate MCP server name');
    if ('command' in server) {
      if (!isAbsolute(server.command)) throw RequestError.invalidParams(undefined, 'MCP executable must be absolute');
      result[server.name] = {transport:'stdio',command:server.command,args:server.args,
        env:Object.fromEntries(server.env.map(e=>[e.name,e.value]))};
    } else {
      let url: URL;
      try {url=new URL(server.url);} catch {throw RequestError.invalidParams(undefined,'Invalid MCP URL');}
      if (!['http:','https:'].includes(url.protocol) || url.username || url.password)
        throw RequestError.invalidParams(undefined,'MCP requires HTTP(S) without embedded credentials');
      result[server.name] = {transport:server.type === 'http'?'streamable_http':'sse',url:server.url,
        headers:Object.fromEntries(server.headers.map(h=>[h.name,h.value]))};
    }
  }
  return result;
}
