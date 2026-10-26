/** Shared Engine implementation for every mode that talks the full HTTP + SSE API (local, cloud). */
import type { ChatEvent, ChatHistoryMessage, ChatRequest, ChatSession, FileDecision, QuestionAnswer } from '@hugagent/chat-sdk';
import type { ActiveRun, CompactSummary, DeliveryMode, Engine, EngineInfo, ModelOption, PermissionMode, SendOptions,
  SessionSummary } from './engine.js';
import { CliError, HugAgentOSHttpError, isAbortError } from './errors.js';
import type { HugAgentOSHttp } from './http.js';

const RECONNECT_ATTEMPTS = 5;
export const STEER_LIMIT = 10_000;

export function toSummary(session: ChatSession): SessionSummary {
  return {
    chatId: session.chat_id,
    title: session.title || '',
    updatedAt: typeof session.updated_at === 'string' ? session.updated_at : undefined,
    projectId: typeof session.project_id === 'string' ? session.project_id : undefined,
  };
}

/** Page size the list endpoints accept for a wanted count. */
export function pageSizeFor(limit: number): number {
  return Math.min(Math.max(limit, 1), 100);
}

/** Newest-first summaries of a page of chats, at most `limit`. */
export function latestSummaries(sessions: ChatSession[], limit: number): SessionSummary[] {
  return newestFirst(sessions.map(toSummary)).slice(0, limit);
}

/** `open` runs once and is shared by concurrent callers; a failed attempt is forgotten so the next call retries. */
export function openOnce(open: () => Promise<void>): () => Promise<void> {
  let pending: Promise<void> | undefined;
  return () => (pending ??= open().catch((error: unknown) => {
    pending = undefined;
    throw error;
  }));
}

export function newestFirst(sessions: SessionSummary[]): SessionSummary[] {
  return [...sessions].sort((a, b) => (b.updatedAt ?? '').localeCompare(a.updatedAt ?? ''));
}

export function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason ?? new DOMException('Aborted', 'AbortError'));
    const timer = setTimeout(() => { signal?.removeEventListener('abort', onAbort); resolve(); }, ms);
    const onAbort = () => { clearTimeout(timer); reject(signal?.reason ?? new DOMException('Aborted', 'AbortError')); };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

/** A dropped stream (not a refusal) that a resume from the last offset can repair. */
function isDisconnect(error: unknown): boolean {
  if (error instanceof HugAgentOSHttpError) return error.status === 0 || error.status === 502 || error.status === 503 || error.status === 504;
  if (!(error instanceof Error)) return false;
  return error.name === 'TypeError' || /SSE disconnected|terminated|ECONNRESET|socket/i.test(error.message);
}

export abstract class HttpEngine implements Engine {
  abstract readonly info: EngineInfo;
  protected client?: HugAgentOSHttp;
  private readonly opening = openOnce(() => this.open());

  /** Start or attach to the backend and set `this.client`. */
  protected abstract open(): Promise<void>;
  abstract newSession(title?: string): Promise<SessionSummary>;
  abstract listSessions(limit?: number): Promise<SessionSummary[]>;

  connect(): Promise<void> {
    return this.opening();
  }

  async close(): Promise<void> {
    // Nothing to release: HTTP is stateless and the backend outlives this client.
  }

  protected get http(): HugAgentOSHttp {
    if (!this.client) throw new CliError('Engine is not connected; call connect() first');
    return this.client;
  }

  upload(chatId: string, file: File, signal?: AbortSignal) {
    return this.http.chat.upload(file,chatId,signal);
  }
  registerClientSession(chatId: string, cwd: string, servers: Record<string, unknown>, mode: PermissionMode = 'ask') {
    return this.http.json<{lease_id: string; ttl_seconds: number}>('POST', `/v1/client-sessions/${encodeURIComponent(chatId)}`, {cwd, mode, servers});
  }
  renewClientSession(chatId: string, leaseId: string, mode?: PermissionMode) {
    return this.http.json('PATCH', `/v1/client-sessions/${encodeURIComponent(chatId)}`, {lease_id: leaseId, mode});
  }
  releaseClientSession(chatId: string, leaseId: string) {
    return this.http.json('DELETE', `/v1/client-sessions/${encodeURIComponent(chatId)}?lease_id=${encodeURIComponent(leaseId)}`);
  }

  async history(chatId: string, limit = 50): Promise<ChatHistoryMessage[]> {
    const pageSize = pageSizeFor(limit);
    const collected: ChatHistoryMessage[] = [];
    // order=desc picks the most recent page first, but each page is still in
    // conversation order, so sort by sequence rather than reversing.
    for (let page = 1; collected.length < limit; page++) {
      const { items } = await this.http.messages(chatId, page, pageSize, 'desc');
      collected.push(...items);
      if (items.length < pageSize) break;
    }
    const seq = (message: ChatHistoryMessage) => (typeof message.chat_seq === 'number' ? message.chat_seq : 0);
    return collected.sort((a, b) => seq(a) - seq(b)).slice(-limit);
  }

  /** Get and verify session ownership through the server, then enforce the local workspace. */
  async getSession(chatId: string): Promise<SessionSummary> {
    const session = toSummary(await this.http.json<ChatSession>('GET', `/v1/chats/${encodeURIComponent(chatId)}`));
    if (this.info.projectId && session.projectId !== this.info.projectId)
      throw new CliError('Session does not belong to this workspace');
    return session;
  }

  async deleteSession(chatId: string): Promise<void> {
    await this.getSession(chatId);
    await this.http.json('DELETE', `/v1/chats/${encodeURIComponent(chatId)}`);
  }

  async sessionPage(page: number, size: number): Promise<SessionSummary[]> {
    const result = this.info.projectId
      ? await this.http.listProjectChats(this.info.projectId, page, size)
      : await this.http.listChats(page, size);
    return result.items.map(toSummary);
  }

  async *historyAll(chatId: string): AsyncGenerator<ChatHistoryMessage> {
    await this.getSession(chatId);
    for (let page = 1; ; page++) {
      const result = await this.http.messages(chatId, page, 100, 'asc');
      yield* result.items;
      if (result.items.length < 100) return;
    }
  }

  async fork(chatId: string, messageId?: string): Promise<SessionSummary> {
    return toSummary(await this.http.fork(chatId, messageId));
  }

  async *send(chatId: string, message: string, options: SendOptions = {}): AsyncIterable<ChatEvent> {
    const request: ChatRequest = { chat_id: chatId, message };
    if (options.clientSessionId) request.client_session_id = options.clientSessionId;
    if (options.attachments?.length) request.attachments = options.attachments;
    if (options.model) {
      request.model_provider_id = options.model.providerId;
      request.model_name = options.model.modelName;
    }
    yield* this.follow(this.http.send(request, options.signal), options.signal);
  }

  resume(runId: string, fromOffset = 0, signal?: AbortSignal): AsyncIterable<ChatEvent> {
    return this.follow(this.http.resume(runId, fromOffset, signal), signal, runId, fromOffset || undefined);
  }

  /** Relays a run's events; a dropped connection resumes from the last offset instead of failing. */
  private async *follow(stream: AsyncIterable<ChatEvent>, signal?: AbortSignal, runId?: string,
    offset?: number): AsyncGenerator<ChatEvent> {
    let source = stream;
    let attempts = 0;
    for (;;) {
      try {
        for await (const event of source) {
          if (event.type === 'run_started' && typeof event.run_id === 'string') runId = event.run_id;
          if (typeof event.event_offset === 'number') {
            if (offset !== undefined && event.event_offset <= offset) continue;
            offset = event.event_offset;
          }
          attempts = 0;
          yield event;
        }
        return;
      } catch (error) {
        if (signal?.aborted || isAbortError(error) || !runId || !isDisconnect(error) || attempts >= RECONNECT_ATTEMPTS) throw error;
        attempts += 1;
        await delay(400 * attempts, signal);
        source = this.http.resume(runId, offset ?? 0, signal);
      }
    }
  }

  runStatus(runId: string) {
    return this.http.json<{status:string}>('GET',`/v1/chat-runs/${encodeURIComponent(runId)}`);
  }

  async activeRun(chatId: string): Promise<ActiveRun | null> {
    const run = await this.http.activeRun(chatId);
    if (!run || typeof run.run_id !== 'string') return null;
    return { runId: run.run_id, offset: typeof run.last_event_offset === 'number' ? run.last_event_offset : undefined };
  }

  cancel(runId: string): Promise<void> {
    return this.http.cancel(runId);
  }

  async steer(runId: string, message: string, mode: DeliveryMode = 'steer'): Promise<void> {
    // chat_steer_queue still caps queued input at 10000 characters (ChatRequest allows 100000).
    if (message.length > STEER_LIMIT) throw new CliError(`Messages sent while a run is working are limited to ${STEER_LIMIT} characters`);
    await this.http.steer(runId, message, mode);
  }

  async confirm(chatId: string, confirmId: string, decision: FileDecision, optionId?: string): Promise<void> {
    await this.http.confirmFile(chatId, confirmId, decision, optionId);
  }

  async answer(chatId: string, requestId: string, answers: QuestionAnswer[]): Promise<void> {
    await this.http.answerQuestion(chatId, requestId, answers);
  }

  async compact(chatId: string): Promise<CompactSummary> {
    try {
      const result = await this.http.compact(chatId);
      return {
        compacted: result.compacted === true,
        tokensBefore: result.tokens_before ?? null,
        tokensAfter: result.tokens_after ?? null,
      };
    } catch (error) {
      // FastAPI's own 404 for an unknown route; a missing chat carries its own message.
      if (error instanceof HugAgentOSHttpError && error.status === 404 && error.message.startsWith('Not Found (')) {
        throw new CliError('This server has no manual compaction endpoint (POST /v1/chats/{id}/compact); update it');
      }
      throw error;
    }
  }

  async listModels(): Promise<ModelOption[]> {
    const caps = await this.http.modelCapabilities();
    const models = caps.user_model_switch?.enabled ? caps.user_model_switch.models ?? [] : [];
    return models
      .filter((model) => model.provider_id && model.model_name)
      .map((model) => ({
        providerId: model.provider_id,
        modelName: model.model_name,
        label: model.display_name || model.model_name,
        current: !!model.is_default,
      }));
  }

  getPermissionMode(): Promise<PermissionMode> {
    return this.http.getToolApproval();
  }

  async setPermissionMode(mode: PermissionMode): Promise<void> {
    await this.http.setToolApproval(mode);
  }
}
