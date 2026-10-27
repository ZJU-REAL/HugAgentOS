/** Stable ACP v1 boundary. Engine startup belongs to session creation, never initialize. */
import { agent, RequestError, PROTOCOL_VERSION } from '@agentclientprotocol/sdk';
import type { ClientCapabilities } from '@agentclientprotocol/sdk';
import type { Mode, SendOptions } from '../engine/engine.js';
import { Sessions } from './sessions.js';
import { configOptions } from './configuration.js';
import { prompt } from './turn.js';
import { safe } from './errors.js';
import { historyUpdates } from './history.js';
import { CLI_VERSION } from '../version.js';

export interface AcpOptions {
  mode: Mode;
  permissionMode?: 'ask'|'auto'|'full';
  server?: string;
  model?: SendOptions['model'];
}

export function createAcpAgent(options: AcpOptions) {
  const sessions = new Sessions(options);
  let initialized = false;
  let capabilities: ClientCapabilities = {};
  const requireInitialized = () => {
    if (!initialized) throw RequestError.invalidRequest(undefined, 'initialize is required');
  };
  const app = agent({ name: 'hugagent' })
    .onRequest('initialize', safe(({ params }) => {
      if (initialized) throw RequestError.invalidRequest(undefined, 'Already initialized');
      if (params.protocolVersion !== PROTOCOL_VERSION)
        throw RequestError.unsupportedProtocolVersion(params.protocolVersion, [PROTOCOL_VERSION]);
      initialized = true;
      capabilities = params.clientCapabilities ?? {};
      return { protocolVersion: PROTOCOL_VERSION, agentInfo: { name: 'hugagent', version: CLI_VERSION },
        agentCapabilities: { mcpCapabilities: {http:true,sse:true}, loadSession: true, sessionCapabilities: { list: {}, resume: {}, close: {}, delete: {} }, promptCapabilities: { embeddedContext: true, image:true } }, authMethods: [] };
    }))
    .onRequest('session/new', safe(async ({ params, client }) => {
      requireInitialized();
      const s=await sessions.create(params,undefined,{client,capabilities});
      await client.notify('session/update',{sessionId:s.id,update:{sessionUpdate:'available_commands_update',availableCommands:[{name:'compact',description:'Compact the session context'}]}});
      return sessions.describe(s);
    }))
    .onRequest('session/load', safe(async ({ params, client }) => {
      requireInitialized();
      if (sessions.entries.has(params.sessionId)) throw RequestError.invalidRequest(undefined, 'Session already loaded');
      const s = await sessions.create(params, params.sessionId, {client, capabilities});
      s.replaying=true;
      try {
        for await (const message of s.engine.historyAll(s.id))
          for (const update of historyUpdates(message)) await client.notify('session/update',{sessionId:s.id,update});
        if(s.closing) throw RequestError.invalidRequest(undefined,"Session closed during replay");
      } catch(error) {if(sessions.entries.has(s.id)) await sessions.detach(s.id);throw error;}
      finally {s.replaying=false;}
      return sessions.describe(s);
    }))
    .onRequest('session/resume', safe(async ({ params, client }) => {
      requireInitialized();
      if (sessions.entries.has(params.sessionId)) throw RequestError.invalidRequest(undefined, 'Session already loaded');
      return sessions.describe(await sessions.create({ ...params, mcpServers: params.mcpServers ?? [] }, params.sessionId, {client, capabilities}));
    }))
    .onRequest('session/list', safe(async ({ params }) => { requireInitialized(); return sessions.list(params.cwd ?? process.cwd(), params.cursor); }))
    .onRequest('session/close', safe(async ({ params }) => { requireInitialized(); await sessions.detach(params.sessionId); return {}; }))
    .onRequest('session/delete', safe(async ({ params }) => { requireInitialized(); await sessions.delete(params.sessionId); return {}; }))
    .onRequest('session/prompt', safe(ctx => { requireInitialized(); return prompt(sessions.get(ctx.params.sessionId), ctx, capabilities); }))
    .onNotification('session/cancel', async ({ params }) => { requireInitialized(); await sessions.cancel(sessions.get(params.sessionId)); })
    .onRequest('session/set_config_option', safe(async ({ params, client }) => {
      requireInitialized();
      const s = await sessions.configure(params.sessionId,params.configId,params.value);
      if(params.configId==='mode') await client.notify('session/update',{sessionId:s.id,update:{sessionUpdate:'current_mode_update',currentModeId:s.mode}});
      const options = configOptions(s);
      await client.notify('session/update', { sessionId: s.id, update: { sessionUpdate: 'config_option_update', configOptions: options } });
      return { configOptions: options };
    }))
    .onRequest('session/set_mode', safe(async ({ params, client }) => {
      requireInitialized();
      const s = await sessions.configure(params.sessionId,'mode',params.modeId);
      await client.notify('session/update', { sessionId: s.id, update: { sessionUpdate: 'current_mode_update', currentModeId: s.mode } });
      await client.notify('session/update',{sessionId:s.id,update:{sessionUpdate:'config_option_update',configOptions:configOptions(s)}});
      return {};
    }));
  return { app, close: () => sessions.close() };
}
