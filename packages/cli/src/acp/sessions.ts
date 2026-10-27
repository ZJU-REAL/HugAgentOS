import { clientTools } from './client-tools.js';
import type {AgentContext, ClientCapabilities} from '@agentclientprotocol/sdk';
import { mcpConfig } from './mcp.js';
import { realpath, stat } from 'node:fs/promises';
import { isAbsolute } from 'node:path';
import { RequestError, type NewSessionRequest } from '@agentclientprotocol/sdk';
import { HttpEngine } from '../engine/base.js';
import { buildEngine } from '../engine/index.js';
import type { Mode, SendOptions } from '../engine/engine.js';
import { configOptions, configure, modes, type Configuration } from './configuration.js';

export interface Session extends Configuration {
  id: string;
  cwd: string;
  engine: HttpEngine;
  leaseId: string;
  bridge?: Awaited<ReturnType<typeof clientTools>>;
  timer?: ReturnType<typeof setInterval>;
  unavailable?: boolean;
  closing?: boolean;
  configuring?: boolean;
  replaying?: boolean;
  active?: { started: Promise<string | undefined>; transport: AbortController; controller: AbortController; runId?: string; done: Promise<void>; cancelling?: Promise<void>; resolve(): void };
}
export class Sessions {
  readonly entries = new Map<string, Session>();
  private closed = false;
  private workspaces = new Map<string,string>();
  private loading = new Set<string>();
  constructor(private options: { mode: Mode; permissionMode?: 'ask'|'auto'|'full'; server?: string; model?: SendOptions['model'] }) {}
  async create(params: NewSessionRequest, existingId?: string, peer?: {client:AgentContext; capabilities:ClientCapabilities}): Promise<Session> {
    if (this.closed) throw RequestError.invalidRequest(undefined, 'Connection closed');
    if (!isAbsolute(params.cwd)) throw RequestError.invalidParams(undefined, 'cwd must be absolute');
    const servers = mcpConfig(params.mcpServers);
    if (params.additionalDirectories?.length) throw RequestError.invalidParams(undefined, 'Additional directories are not available');
    let cwd = params.cwd;
    if (this.options.mode === 'local') {
      try { cwd = await realpath(cwd); if (!(await stat(cwd)).isDirectory()) throw new Error(); }
      catch { throw RequestError.invalidParams(undefined, 'cwd must be an existing directory'); }
    }
    if (existingId && (this.loading.has(existingId) || this.entries.has(existingId))) throw RequestError.invalidRequest(undefined, 'Session already loading or loaded');
    if (existingId) this.loading.add(existingId);
    const engine = buildEngine(this.options.mode, { cwd }, { server: this.options.server });
    if (!(engine instanceof HttpEngine)) throw RequestError.invalidParams(undefined, 'ACP requires local or cloud mode');
    let state: Session | undefined;
    try {
      await engine.connect();
      const models = await engine.listModels();
      const model = this.options.model ?? models.find(m => m.current) ?? models[0];
      if (model && !models.some(m => m.providerId === model.providerId && m.modelName === model.modelName))
        models.push({ ...model, label: model.modelName });
      const created = existingId ? await engine.getSession(existingId) : await engine.newSession();
      if (this.closed) throw RequestError.invalidRequest(undefined, 'Connection closed');
      state = { id: created.chatId, cwd, engine, models, model, mode: this.options.permissionMode ?? 'ask', leaseId: '' };
      if (peer) state.bridge = await clientTools(state,peer.client,peer.capabilities);
      if (state.bridge) servers.acp_client = state.bridge.config;
      const lease = await engine.registerClientSession(created.chatId, cwd, servers, state.mode);
      state.leaseId = lease.lease_id;
      const owned = state;
      let renewing = false;
      state.timer = setInterval(async () => {
        if (renewing || owned.closing) return;
        renewing = true;
        try { await engine.renewClientSession(owned.id, owned.leaseId); }
        catch { owned.unavailable = true; await cancelTurn(owned).catch(() => {}); }
        finally { renewing = false; }
      }, Math.min(60000, lease.ttl_seconds * 1000 / 3));
      state.timer.unref();
      if (this.closed) {
        clearInterval(state.timer);
        await engine.releaseClientSession(state.id, state.leaseId);
        throw RequestError.invalidRequest(undefined, 'Connection closed');
      }
      this.entries.set(state.id, state);
      this.workspaces.set(state.id,cwd);
      return state;
    } catch (error) { await state?.bridge?.close(); await engine.close(); throw error; }
    finally { if (existingId) this.loading.delete(existingId); }
  }
  get(id: string): Session {
    const s = this.entries.get(id);
    if (!s) throw RequestError.resourceNotFound(id);
    return s;
  }
  describe(s: Session) {
    return { sessionId: s.id, configOptions: configOptions(s), modes: { currentModeId: s.mode, availableModes: modes } };
  }
  async cancel(s: Session): Promise<void> {
    await cancelTurn(s);
  }

  async configure(id:string, key:string, value:unknown) {
    const s=this.get(id);
    if(s.active || s.closing || s.configuring || s.replaying || s.unavailable)
      throw RequestError.invalidRequest(undefined,'Session is busy or unavailable');
    s.configuring=true;
    try {
      const next={...s};configure(next,key,value);
      await s.engine.renewClientSession(s.id,s.leaseId,next.mode);
      s.mode=next.mode;s.model=next.model;
      return s;
    } finally {s.configuring=false;}
  }
  async detach(id: string, remove = false): Promise<void> {
    const s = this.get(id);
    if (s.closing) throw RequestError.invalidRequest(undefined, 'Session is closing');
    s.closing = true;
    clearInterval(s.timer);
    try {
      await this.cancel(s);
      await s.active?.done;
      await s.engine.releaseClientSession(s.id, s.leaseId);
      if (remove) await s.engine.deleteSession(id);
    } finally {
      this.entries.delete(id);
      try {await s.bridge?.close();} finally {await s.engine.close();}
    }
  }
  async delete(id: string) {
    if (this.entries.has(id)) return this.detach(id,true);
    if (this.loading.has(id)) throw RequestError.invalidRequest(undefined,'Session is loading');
    this.loading.add(id);
    const cwd=this.workspaces.get(id) ?? process.cwd();
    const engine=buildEngine(this.options.mode,{cwd},{server:this.options.server});
    try {
      if (!(engine instanceof HttpEngine)) throw RequestError.invalidRequest();
      await engine.connect();
      if (await engine.activeRun(id)) throw RequestError.invalidRequest(undefined,'Close the active session before deleting');
      await engine.deleteSession(id);
      this.workspaces.delete(id);
    } finally {this.loading.delete(id);await engine.close();}
  }
  async list(cwd: string, cursor?: string | null) {
    if (!isAbsolute(cwd)) throw RequestError.invalidParams(undefined, 'cwd must be absolute');
    let page = 1;
    if (cursor) {
      try {
        const parsed = JSON.parse(Buffer.from(cursor, 'base64url').toString());
        if (parsed.cwd !== cwd || !Number.isSafeInteger(parsed.page) || parsed.page < 1) throw new Error();
        page = parsed.page;
      } catch { throw RequestError.invalidParams(undefined, 'Invalid session cursor'); }
    }
    const engine = buildEngine(this.options.mode, { cwd }, { server: this.options.server });
    if (!(engine instanceof HttpEngine)) throw RequestError.invalidParams(undefined, 'ACP requires local or cloud mode');
    try {
      await engine.connect();
      const rows = await engine.sessionPage(page, 50);
      for (const row of rows) this.workspaces.set(row.chatId,cwd);
      return { sessions: rows.map(s => ({ sessionId: s.chatId, cwd, title: s.title, updatedAt: s.updatedAt })),
        nextCursor: rows.length === 50 ? Buffer.from(JSON.stringify({ cwd, page: page + 1 })).toString('base64url') : undefined };
    } finally { await engine.close(); }
  }
  async close(): Promise<void> {
    this.closed = true;
    await Promise.allSettled([...this.entries.values()].map(async s => {
      s.closing = true;
      clearInterval(s.timer);
      try { await this.cancel(s); await s.active?.done; }
      finally { try {await s.engine.releaseClientSession(s.id, s.leaseId);} finally {await s.bridge?.close(); await s.engine.close();} }
    }));
    this.entries.clear();
  }
}

export function cancelTurn(s: Session): Promise<void> {
  const active = s.active;
  if (!active) return Promise.resolve();
  if (!active.cancelling) {
    active.cancelling = (async () => {
      const id = active.runId ?? await active.started;
      try { if (id) await s.engine.cancel(id); }
      catch (error) {
        s.unavailable = true;
        clearInterval(s.timer);
        await s.engine.releaseClientSession(s.id, s.leaseId);
        throw error;
      }
      finally { active.transport.abort(); }
    })();
    active.controller.abort();
  }
  return active.cancelling;
}
