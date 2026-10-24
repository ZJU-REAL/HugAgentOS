/**
 * One authenticated HTTP layer for every engine: `ChatClient` from @hugagent/chat-sdk for the
 * streaming and confirmation calls, plus the HugAgentOS endpoints the SDK does not cover.
 * Every JSON call unwraps the `{code, data}` envelope; failures become HugAgentOSHttpError with
 * the HTTP status and the server's own message.
 */
import { randomUUID } from 'node:crypto';
import { ChatApiError, ChatClient, type ChatEvent, type ChatHistoryMessage, type ChatRequest, type ChatSession,
  type FileDecision, type QuestionAnswer } from '@hugagent/chat-sdk';
import type { DeliveryMode, PermissionMode } from './engine.js';
import { HugAgentOSHttpError } from './errors.js';

const JSON_TIMEOUT_MS = 30_000;

export interface HttpOptions {
  /** API root, e.g. http://127.0.0.1:8123 or https://agent.example.com/api. */
  baseUrl: string;
  token: string;
  fetch?: typeof fetch;
}

export interface LocalProject { project_id: string; name: string; local_path: string; created: boolean }
export interface CompactResult { compacted: boolean; tokens_before: number | null; tokens_after: number | null }
export interface RunStatus { run_id: string; chat_id: string; message_id?: string; status: string }
export interface ActiveRunInfo { run_id: string; message_id?: string; status?: string; last_event_offset?: number }
export interface SelectableModel {
  provider_id: string;
  display_name: string;
  model_name: string;
  provider?: string;
  is_default?: boolean;
}
export interface ModelCapabilities {
  main_agent?: Record<string, unknown>;
  user_model_switch?: { enabled?: boolean; models?: SelectableModel[] };
  [key: string]: unknown;
}
export interface Page<T> { items: T[]; [key: string]: unknown }
/** A session-manager operation receipt (personal_agent/sessions/session_queue.py `public`). */
export interface OperationJob {
  id: string;
  target?: string;
  operation?: string;
  status: string;
  result?: unknown;
  deadline?: number;
  [key: string]: unknown;
}

/** Turns any error body HugAgentOS produces into one readable line. */
export function describeErrorBody(status: number, body: unknown): { message: string; code?: number; detail?: unknown } {
  const fallback = { message: 'HTTP ' + status };
  if (!body || typeof body !== 'object') return fallback;
  const record = body as Record<string, unknown>;
  const code = typeof record.code === 'number' ? record.code : undefined;
  // error_response(): {code, message, data}
  if (typeof record.message === 'string' && record.message) {
    return { message: record.message, code, detail: record.data ?? record.detail };
  }
  const detail = record.detail;
  if (typeof detail === 'string' && detail) return { message: detail, code, detail };
  if (Array.isArray(detail)) {
    // FastAPI validation: [{loc: [...], msg}]
    const parts = detail.map((item) => {
      const entry = item && typeof item === 'object' ? item as Record<string, unknown> : {};
      const loc = Array.isArray(entry.loc) ? entry.loc.filter((part) => part !== 'body').join('.') : '';
      const msg = typeof entry.msg === 'string' ? entry.msg : JSON.stringify(item);
      return loc ? loc + ': ' + msg : msg;
    });
    return { message: parts.join('; ') || fallback.message, code, detail };
  }
  if (detail && typeof detail === 'object') {
    const nested = detail as Record<string, unknown>;
    const message = typeof nested.message === 'string' ? nested.message
      : typeof nested.error === 'string' ? nested.error : JSON.stringify(detail);
    return { message, code: typeof nested.code === 'number' ? nested.code : code, detail };
  }
  if (typeof record.error === 'string') return { message: record.error, code, detail: record };
  return fallback;
}

function httpError(status: number, body: unknown): HugAgentOSHttpError {
  const { message, code, detail } = describeErrorBody(status, body);
  return new HugAgentOSHttpError(message + ' (HTTP ' + status + ')', status, code, detail);
}

/** ChatClient only reads `message`; rebuild the full description from what it kept. */
function translate(error: unknown): unknown {
  if (!(error instanceof ChatApiError)) return error;
  const body = /^HTTP \d+$/.test(error.message) ? { detail: error.detail } : { message: error.message, detail: error.detail };
  const { message, code, detail } = describeErrorBody(error.status, body);
  // An envelope code the SDK kept wins, with the detail it came with.
  const known = error.code !== undefined;
  return new HugAgentOSHttpError(message + ' (HTTP ' + error.status + ')', error.status,
    known ? error.code : code, known ? error.detail : detail);
}

/** An SDK call with its error translated like every other call here. */
function viaSdk<T>(call: Promise<T>): Promise<T> {
  return call.catch((error: unknown) => { throw translate(error); });
}

async function* translated<T>(source: AsyncIterable<T>): AsyncGenerator<T> {
  try {
    yield* source;
  } catch (error) {
    throw translate(error);
  }
}

const seg = (value: string) => {
  if (!value || !value.trim()) throw new Error('Identifier must not be empty');
  return encodeURIComponent(value);
};

export class HugAgentOSHttp {
  readonly baseUrl: string;
  readonly chat: ChatClient;
  private readonly fetcher: typeof fetch;

  constructor(private readonly options: HttpOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, '');
    const base = options.fetch ?? globalThis.fetch.bind(globalThis);
    // Credentials never follow a redirect to another host.
    this.fetcher = ((input: RequestInfo | URL, init?: RequestInit) => base(input, { ...init, redirect: 'error' })) as typeof fetch;
    this.chat = new ChatClient({
      baseUrl: this.baseUrl,
      headers: () => this.headers(),
      credentials: 'omit',
      fetch: this.fetcher,
    });
  }

  private headers(): Record<string, string> {
    return { Authorization: 'Bearer ' + this.options.token, Accept: 'application/json' };
  }

  /** One JSON call: envelope unwrapped, errors carry status + server detail. */
  async json<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    const timeout = AbortSignal.timeout(JSON_TIMEOUT_MS);
    const headers: Record<string, string> = this.headers();
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let response: Response;
    try {
      response = await this.fetcher(this.baseUrl + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      if (timeout.aborted) throw new HugAgentOSHttpError(`${method} ${path} timed out`, 0);
      throw new HugAgentOSHttpError(`Cannot reach ${this.baseUrl}: ${causeOf(error)}`, 0);
    }
    const text = await response.text();
    let parsed: unknown = undefined;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        if (!response.ok) throw new HugAgentOSHttpError(`${text.slice(0, 200)} (HTTP ${response.status})`, response.status);
        throw new HugAgentOSHttpError(`Invalid JSON from ${method} ${path} (HTTP ${response.status})`, response.status);
      }
    }
    if (!response.ok) throw httpError(response.status, parsed);
    if (parsed === undefined) return undefined as T;
    if (parsed && typeof parsed === 'object' && 'code' in parsed && 'data' in parsed) {
      const envelope = parsed as { code: unknown; data: T; message?: string };
      if (typeof envelope.code === 'number' && (envelope.code < 10000 || envelope.code >= 20000)) {
        throw new HugAgentOSHttpError((envelope.message || 'API request failed') + ' (code ' + envelope.code + ')',
          response.status, envelope.code, envelope.data);
      }
      return envelope.data;
    }
    return parsed as T;
  }

  /** GET /health on the server root, never throws. */
  async health(timeoutMs = 3000): Promise<boolean> {
    try {
      const response = await this.fetcher(this.baseUrl + '/health', { signal: AbortSignal.timeout(timeoutMs) });
      await response.body?.cancel().catch(() => {});
      return response.status === 200;
    } catch {
      return false;
    }
  }

  // ── Sessions ────────────────────────────────────────────────────────────

  createChat(data: { title?: string; projectId?: string }, signal?: AbortSignal) {
    const body: Record<string, unknown> = {};
    if (data.title) body.title = data.title;
    if (data.projectId) body.project_id = data.projectId;
    return this.json<ChatSession>('POST', '/v1/chats', body, signal);
  }

  listChats(page = 1, pageSize = 20, signal?: AbortSignal) {
    return this.json<Page<ChatSession>>('GET', '/v1/chats?' + query({ page, page_size: pageSize, exclude_automation: true }), undefined, signal);
  }

  listProjectChats(projectId: string, page = 1, pageSize = 20, signal?: AbortSignal) {
    return this.json<Page<ChatSession>>('GET', `/v1/projects/${seg(projectId)}/chats?` + query({ page, page_size: pageSize, scope: 'mine' }), undefined, signal);
  }

  /** `order=desc` makes page 1 the latest messages. */
  messages(chatId: string, page = 1, pageSize = 50, order: 'asc' | 'desc' = 'desc', signal?: AbortSignal) {
    return this.json<Page<ChatHistoryMessage>>('GET', `/v1/chats/${seg(chatId)}/messages?` + query({ page, page_size: pageSize, order }), undefined, signal);
  }

  fork(chatId: string, throughMessageId?: string) {
    const body: Record<string, unknown> = { request_id: randomUUID() };
    if (throughMessageId) body.through_message_id = throughMessageId;
    return this.json<ChatSession>('POST', `/v1/chats/${seg(chatId)}/fork`, body);
  }

  compact(chatId: string) {
    return this.json<CompactResult>('POST', `/v1/chats/${seg(chatId)}/compact`, {});
  }

  /** Bind a folder to its local project, creating it, and grant it to the OS sandbox. */
  resolveLocalProject(path: string) {
    return this.json<LocalProject>('POST', '/v1/projects/local/resolve', { path, create: true, grant: true });
  }

  // ── Runs ────────────────────────────────────────────────────────────────

  send(request: ChatRequest, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
    return translated(this.chat.send(request, signal));
  }

  resume(runId: string, from = 0, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
    return translated(this.chat.resume(runId, from, signal));
  }

  activeRun(chatId: string, signal?: AbortSignal): Promise<ActiveRunInfo | null> {
    return viaSdk(this.chat.activeRun(chatId, signal)) as Promise<ActiveRunInfo | null>;
  }

  async cancel(runId: string): Promise<void> {
    await viaSdk(this.chat.cancel(runId));
  }

  /** Each steer gets a fresh steer_id; the server de-duplicates retries by it. */
  steer(runId: string, message: string, mode: DeliveryMode = 'steer') {
    return this.json<Record<string, unknown>>('POST', `/v1/chat-runs/${seg(runId)}/steer`, {
      steer_id: randomUUID().replace(/-/g, ''), message, delivery_mode: mode,
    });
  }

  confirmFile(chatId: string, confirmId: string, decision: FileDecision, optionId?: string) {
    return viaSdk(this.chat.confirmFile(chatId, confirmId, decision, optionId));
  }

  answerQuestion(chatId: string, requestId: string, answers: QuestionAnswer[]) {
    return viaSdk(this.chat.answerQuestion(chatId, requestId, answers));
  }

  // ── Settings ────────────────────────────────────────────────────────────

  /** The web model picker's source (frontend api.ts getMainModelCapabilities). */
  modelCapabilities() {
    return this.json<ModelCapabilities>('GET', '/v1/models/capabilities');
  }

  async getToolApproval(): Promise<PermissionMode> {
    return (await this.json<{ mode: PermissionMode }>('GET', '/v1/tool-approval')).mode;
  }

  async setToolApproval(mode: PermissionMode): Promise<PermissionMode> {
    return (await this.json<{ mode: PermissionMode }>('PUT', '/v1/tool-approval', { mode })).mode;
  }

  // ── Session-manager queue (device:<id>) ─────────────────────────────────

  submitOperation(target: string, operation: string, args: Record<string, unknown>) {
    return this.json<OperationJob>('POST', '/v1/personal-agent/session-manager/operations', {
      target, operation, args, request_key: randomUUID().replace(/-/g, ''),
    });
  }

  getOperation(id: string, signal?: AbortSignal) {
    return this.json<OperationJob>('GET', `/v1/personal-agent/session-manager/operations/${seg(id)}`, undefined, signal);
  }

  listDevices() {
    return this.json<unknown>('GET', '/v1/personal-agent/session-manager/devices');
  }
}

function query(values: Record<string, string | number | boolean>): string {
  return new URLSearchParams(Object.entries(values).map(([key, value]) => [key, String(value)])).toString();
}

function causeOf(error: unknown): string {
  if (error instanceof Error) {
    const cause = (error as { cause?: unknown }).cause;
    if (cause instanceof Error) return cause.message;
    return error.message;
  }
  return String(error);
}
