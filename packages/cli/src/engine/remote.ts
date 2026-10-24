/**
 * RemoteEngine (cloud) talks to a remote HugAgentOS over the full HTTP + SSE API.
 * DeviceEngine (device:<id>) drives another paired computer through the session-manager
 * operation queue (src/backend/hugagent_cli/contract.py); that link has no SSE, so a turn is
 * synthesized from run.status polls and the final chat.history message.
 */
import { randomUUID } from 'node:crypto';
import type { ChatEvent, ChatHistoryMessage, ChatSession } from '@hugagent/chat-sdk';
import { HttpEngine, delay, latestSummaries, openOnce, pageSizeFor, toSummary } from './base.js';
import type { ActiveRun, CompactSummary, DeliveryMode, Engine, EngineInfo, Mode, ModelOption, PermissionMode, SendOptions,
  SessionSummary } from './engine.js';
import { CliError, HugAgentOSHttpError, SetupRequiredError, UnsupportedError } from './errors.js';
import { HugAgentOSHttp, describeErrorBody, type Page, type RunStatus } from './http.js';

export interface RemoteOptions {
  server: string;
  apiKey: string;
  fetch?: typeof fetch;
}

function hostOf(server: string): string {
  try {
    const url = new URL(server);
    return url.host + (url.pathname !== '/' ? url.pathname.replace(/\/+$/, '') : '');
  } catch {
    return server;
  }
}

function rejected(error: unknown, server: string): unknown {
  if (error instanceof HugAgentOSHttpError && (error.status === 401 || error.status === 403)) {
    return new SetupRequiredError(`${server} rejected the API key (${error.message}): run \`hugagent login --server ${server}\``);
  }
  return error;
}

export class RemoteEngine extends HttpEngine {
  readonly info: EngineInfo;

  constructor(private readonly options: RemoteOptions) {
    super();
    this.info = { mode: 'cloud', label: 'cloud · ' + hostOf(options.server), server: options.server, canAccessCwd: false };
  }

  protected async open(): Promise<void> {
    const http = new HugAgentOSHttp({ baseUrl: this.options.server, token: this.options.apiKey, fetch: this.options.fetch });
    try {
      // Any authenticated read proves the key before the first real request.
      await http.getToolApproval();
    } catch (error) {
      throw rejected(error, this.options.server);
    }
    this.client = http;
  }

  async newSession(title?: string): Promise<SessionSummary> {
    await this.connect();
    return toSummary(await this.http.createChat({ title }));
  }

  async listSessions(limit = 20): Promise<SessionSummary[]> {
    await this.connect();
    const page = await this.http.listChats(1, pageSizeFor(limit));
    return latestSummaries(page.items, limit);
  }
}

const OPERATION_WAIT_MS = 60_000;
const RUN_POLL_MS = 2_000;
const DEVICE_MESSAGE_LIMIT = 10_000;
const LIVE_RUN = new Set(['pending', 'queued', 'running']);

export class DeviceEngine implements Engine {
  readonly info: EngineInfo;
  private readonly http: HugAgentOSHttp;
  private readonly opening = openOnce(() => this.open());

  constructor(mode: Mode, private readonly options: RemoteOptions) {
    this.http = new HugAgentOSHttp({ baseUrl: options.server, token: options.apiKey, fetch: options.fetch });
    this.info = { mode, label: `${mode} · via ${hostOf(options.server)}`, server: options.server, canAccessCwd: false };
  }

  connect(): Promise<void> {
    return this.opening();
  }

  private async open(): Promise<void> {
    let devices: unknown;
    try {
      devices = await this.http.listDevices();
    } catch (error) {
      throw rejected(error, this.options.server);
    }
    const id = this.info.mode.slice('device:'.length);
    const list = Array.isArray(devices) ? devices
      : devices && typeof devices === 'object' && Array.isArray((devices as Page<unknown>).items) ? (devices as Page<unknown>).items : null;
    if (list) {
      const ids = list.flatMap((device) => {
        const row = device && typeof device === 'object' ? device as Record<string, unknown> : {};
        return [row.id, row.device_id].filter((value): value is string => typeof value === 'string');
      });
      if (ids.length && !ids.includes(id)) throw new CliError(`No paired device ${id}; paired: ${ids.join(', ')}`);
    }
  }

  async close(): Promise<void> {}

  /** Queue one operation and wait for the device's receipt. */
  private async op<T>(operation: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<T> {
    await this.connect();
    let job = await this.http.submitOperation(this.info.mode, operation, args);
    if (!job || typeof job.id !== 'string') return job as T;
    const deadline = Date.now() + OPERATION_WAIT_MS;
    let wait = 300;
    while ((job.status === 'queued' || job.status === 'sent') && Date.now() < deadline) {
      await delay(wait, signal);
      wait = Math.min(wait * 1.5, 1000);
      job = await this.http.getOperation(job.id, signal);
    }
    if (job.status === 'queued' || job.status === 'sent') {
      throw new CliError(`${this.info.mode} did not answer ${operation} within ${OPERATION_WAIT_MS / 1000} s (operation ${job.id}); is the device online with sessions enabled?`);
    }
    if (job.status !== 'completed') throw new CliError(`${operation} on ${this.info.mode}: ${job.status} (operation ${job.id})`);
    const result = job.result;
    if (result && typeof result === 'object' && 'error' in result && (result as Record<string, unknown>).error) {
      const record = result as Record<string, unknown>;
      const status = typeof record.http_status === 'number' ? record.http_status : 0;
      const { message } = describeErrorBody(status, record);
      throw new HugAgentOSHttpError(`${operation} failed on ${this.info.mode}: ${message}`, status, undefined, record);
    }
    return result as T;
  }

  async newSession(title?: string): Promise<SessionSummary> {
    return toSummary(await this.op<ChatSession>('chat.create', title ? { title } : {}));
  }

  async listSessions(limit = 20): Promise<SessionSummary[]> {
    const page = await this.op<Page<ChatSession>>('chat.list', { page: 1, page_size: pageSizeFor(limit) });
    return latestSummaries(page?.items ?? [], limit);
  }

  async history(chatId: string, limit = 50): Promise<ChatHistoryMessage[]> {
    const page = await this.op<Page<ChatHistoryMessage>>('chat.history', {
      chat_id: chatId, page: 1, page_size: pageSizeFor(limit), order: 'desc',
    });
    return (page?.items ?? []).slice(0, limit).reverse();
  }

  async *send(chatId: string, message: string, options: SendOptions = {}): AsyncIterable<ChatEvent> {
    if (options.model) throw new UnsupportedError('Per-message model selection is not available in device mode');
    if (options.attachments?.length) throw new UnsupportedError('Attachments are not available in device mode');
    if (message.length > DEVICE_MESSAGE_LIMIT) throw new UnsupportedError(`Device messages are limited to ${DEVICE_MESSAGE_LIMIT} characters`);
    const started = await this.op<Record<string, unknown>>('chat.send', { chat_id: chatId, message }, options.signal);
    const runId = typeof started?.run_id === 'string' ? started.run_id : '';
    if (!runId) throw new CliError(`${this.info.mode} accepted the message but returned no run id`);
    yield { ...started, type: 'run_started', run_id: runId, chat_id: chatId };
    yield* this.followRun(runId, chatId, options.signal);
  }

  resume(runId: string, _fromOffset?: number, signal?: AbortSignal): AsyncIterable<ChatEvent> {
    return this.followRun(runId, undefined, signal);
  }

  /** Poll run.status until the run settles, then emit its answer from chat.history. */
  private async *followRun(runId: string, chatId: string | undefined, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
    let status: RunStatus;
    for (;;) {
      status = await this.op<RunStatus>('run.status', { run_id: runId }, signal);
      if (!LIVE_RUN.has(status?.status)) break;
      await delay(RUN_POLL_MS, signal);
    }
    const chat = chatId ?? status.chat_id;
    if (chat) {
      const recent = await this.op<Page<ChatHistoryMessage>>('chat.history', { chat_id: chat, page: 1, page_size: 10, order: 'desc' }, signal);
      const items = recent?.items ?? [];
      const answer = items.find((item) => item.role === 'assistant' && (!status.message_id || item.message_id === status.message_id))
        ?? items.find((item) => item.role === 'assistant');
      if (answer?.content) yield { type: 'content', content: answer.content, message_id: answer.message_id, run_id: runId };
    }
    if (status.status === 'failed' || status.status === 'needs_attention') {
      yield { type: 'error', error: status.status === 'failed' ? 'Run failed on the device' : 'Run needs attention on the device', run_id: runId };
    }
    yield { type: 'end', run_id: runId, status: status.status };
  }

  async activeRun(chatId: string): Promise<ActiveRun | null> {
    const run = await this.op<Record<string, unknown> | null>('run.active', { chat_id: chatId });
    return run && typeof run.run_id === 'string' ? { runId: run.run_id } : null;
  }

  async cancel(runId: string): Promise<void> {
    await this.op('run.cancel', { run_id: runId });
  }

  async steer(runId: string, message: string, mode: DeliveryMode = 'steer'): Promise<void> {
    await this.op('run.steer', { run_id: runId, message, steer_id: randomUUID().replace(/-/g, ''), delivery_mode: mode });
  }

  private unsupported(what: string): never {
    throw new UnsupportedError(`${what} is not available in device mode; switch with /mode local or /mode cloud`);
  }

  async fork(): Promise<SessionSummary> { this.unsupported('Forking a session'); }
  async confirm(): Promise<void> { this.unsupported('Approving from this terminal'); }
  async answer(): Promise<void> { this.unsupported('Answering questions from this terminal'); }
  async compact(): Promise<CompactSummary> { this.unsupported('Manual compaction'); }
  async listModels(): Promise<ModelOption[]> { this.unsupported('Model selection'); }
  async getPermissionMode(): Promise<PermissionMode> { this.unsupported('Permission modes'); }
  async setPermissionMode(): Promise<void> { this.unsupported('Permission modes'); }
}
