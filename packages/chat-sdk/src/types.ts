/** Public wire contracts. Extra event fields are preserved for forward compatibility. */
export interface ChatEvent { type: string; [key: string]: unknown }
export interface ChatSession { chat_id: string; title: string; [key: string]: unknown }
export interface ChatHistoryMessage { message_id: string; role: string; content: string; chat_seq?: number; [key: string]: unknown }
export interface ChatPage<T> { items: T[]; [key: string]: unknown }
export interface UploadedFile { file_id: string; name: string; download_url: string; mime_type: string; size: number }
export interface ChatRequest { chat_id: string; message: string; attachments?: { file_id: string; name: string; mime_type: string }[]; [key: string]: unknown }
export interface ConfirmationResult { ok: boolean; stale?: boolean; chat_interrupted?: boolean; message?: string }
export type FileDecision = 'allow' | 'allow_session' | 'deny' | 'choice' | 'skip';
export interface QuestionAnswer { id: string; selected: string[]; custom?: string; skipped?: boolean }
export interface ChatClientOptions {
  /** Includes /api when using the default nginx gateway. */
  baseUrl: string;
  /** Executed on each request, so token refresh does not recreate the client. */
  headers?: () => HeadersInit | Promise<HeadersInit>;
  credentials?: RequestCredentials;
  fetch?: typeof fetch;
}
export class ChatApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly code?: number,
    public readonly detail?: unknown) { super(message); this.name = 'ChatApiError'; }
}
