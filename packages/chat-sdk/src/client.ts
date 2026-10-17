import { ChatApiError, type ChatClientOptions, type ChatEvent, type ChatHistoryMessage, type ChatPage,
  type ChatRequest, type ChatSession, type ConfirmationResult, type FileDecision, type QuestionAnswer,
  type UploadedFile } from './types.js';
import { readSSE } from './sse.js';

const id = (value: string) => {
  if (!value.trim()) throw new Error('Identifier must not be empty');
  return encodeURIComponent(value);
};
export class ChatClient {
  private readonly base: string;
  private readonly fetcher: typeof fetch;
  constructor(private readonly options: ChatClientOptions) {
    if (!options.baseUrl || /[?#]/.test(options.baseUrl)) throw new Error('Invalid API base URL');
    this.base = options.baseUrl.replace(/\/+$/, '');
    this.fetcher = options.fetch ?? globalThis.fetch.bind(globalThis);
  }
  private async request(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(await this.options.headers?.());
    new Headers(init.headers).forEach((value, key) => headers.set(key, value));
    if (typeof init.body === 'string') headers.set('Content-Type', 'application/json');
    const response = await this.fetcher(this.base + path, {
      ...init, headers, credentials: this.options.credentials ?? 'include',
    });
    if (!response.ok) {
      const body: unknown = await response.json().catch(() => null);
      const error = body && typeof body === 'object' ? body as Record<string, unknown> : {};
      throw new ChatApiError(typeof error.message === 'string' ? error.message : 'HTTP ' + response.status,
        response.status, typeof error.code === 'number' ? error.code : undefined, error.detail);
    }
    return response;
  }
  private async json<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await this.request(path, init);
    const body: unknown = await response.json();
    if (!body || typeof body !== 'object' || !('data' in body)) throw new Error('Invalid API envelope');
    const envelope = body as { code?: number; data: T; message?: string };
    if (typeof envelope.code !== 'number' || envelope.code < 10000 || envelope.code >= 20000) {
      throw new ChatApiError(envelope.message || 'API request failed', response.status, envelope.code);
    }
    return envelope.data;
  }
  createChat(data: { title?: string; metadata?: Record<string, unknown> } = {}, signal?: AbortSignal) {
    return this.json<ChatSession>('/v1/chats', { method: 'POST', body: JSON.stringify(data), signal });
  }
  chats(page = 1, pageSize = 50, signal?: AbortSignal) {
    return this.json<ChatPage<ChatSession>>('/v1/chats?' + this.pageQuery(page, pageSize), { signal });
  }
  messages(chatId: string, page = 1, pageSize = 50, signal?: AbortSignal) {
    return this.json<ChatPage<ChatHistoryMessage>>('/v1/chats/' + id(chatId) + '/messages?' + this.pageQuery(page, pageSize), { signal });
  }
  private pageQuery(page: number, pageSize: number) {
    if (!Number.isSafeInteger(page) || page < 1 || !Number.isSafeInteger(pageSize) || pageSize < 1 || pageSize > 100) {
      throw new Error('Invalid pagination');
    }
    return new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  }
  async *send(request: ChatRequest, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
    id(request.chat_id);
    const response = await this.request('/v1/agents/responses', {
      method: 'POST', body: JSON.stringify({ ...request, stream: true }), signal,
    });
    yield* readSSE(response, signal);
  }
  async *resume(runId: string, from = 0, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
    if (!Number.isSafeInteger(from) || from < 0) throw new Error('Invalid event offset');
    const response = await this.request('/v1/chats/stream/' + id(runId) + '?from=' + from, { signal });
    yield* readSSE(response, signal);
  }
  async *subscribe(runId: string, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
    const response = await this.request('/v1/chats/stream/' + id(runId) + '/subscription', { signal });
    yield* readSSE(response, signal);
  }
  activeRun(chatId: string, signal?: AbortSignal) {
    return this.json<Record<string, unknown> | null>('/v1/chats/' + id(chatId) + '/active-run', { signal });
  }
  cancel(runId: string, signal?: AbortSignal) {
    return this.json<unknown>('/v1/chat-runs/' + id(runId) + '/cancel', { method: 'POST', signal });
  }
  async upload(file: File, chatId?: string, signal?: AbortSignal): Promise<UploadedFile> {
    const form = new FormData();
    form.append('file', file, file.name);
    if (chatId) form.append('chat_id', chatId);
    const response = await this.request('/v1/file/upload', { method: 'POST', body: form, signal });
    const value: unknown = await response.json();
    if (!value || typeof value !== 'object' || !('file_id' in value) || typeof value.file_id !== 'string') {
      throw new Error('Invalid upload response');
    }
    return value as UploadedFile;
  }
  confirmFile(chatId: string, confirmId: string, decision: FileDecision, optionId?: string) {
    return this.json<ConfirmationResult>('/v1/chats/' + id(chatId) + '/file-confirm', {
      method: 'POST', body: JSON.stringify({ confirm_id: confirmId, decision, ...(optionId ? { option_id: optionId } : {}) }),
    });
  }
  pendingConfirms(chatId: string) {
    return this.json<{ pendings: Record<string, unknown>[] }>('/v1/chats/' + id(chatId) + '/pending-confirm');
  }
  pendingQuestions(chatId: string) {
    return this.json<{ requests: Record<string, unknown>[] }>('/v1/chats/' + id(chatId) + '/pending-user-questions');
  }
  answerQuestion(chatId: string, requestId: string, answers: QuestionAnswer[]) {
    return this.json<ConfirmationResult>('/v1/chats/' + id(chatId) + '/user-questions/' + id(requestId) + '/answer',
      { method: 'POST', body: JSON.stringify({ answers }) });
  }
}
