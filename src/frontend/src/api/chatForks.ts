import { authFetch, chatTargetHeaders, getApiUrl, unwrapData } from '../api';

export interface ChatForkOrigin {
  source_chat_id: string;
  source_message_id: string;
  source_chat_seq: number;
  source_title: string;
  message_count: number;
  created_at: string;
}

export interface ChatForkSession {
  chat_id: string;
  title: string;
  user_id: string;
  project_id: string | null;
  created_at: string;
  updated_at: string;
  message_count: number;
  metadata: Record<string, unknown> & { fork: ChatForkOrigin };
}

export class ChatForkError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ChatForkError';
    this.status = status;
  }
}

/** Requests retain the source execution plane; the server copies the complete prefix. */
export async function createChatFork(
  chatId: string,
  requestId: string,
  throughMessageId?: string,
): Promise<ChatForkSession> {
  const response = await authFetch(`${getApiUrl()}/v1/chats/${encodeURIComponent(chatId)}/fork`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...chatTargetHeaders(chatId) },
    body: JSON.stringify({ request_id: requestId, ...(throughMessageId ? { through_message_id: throughMessageId } : {}) }),
  });
  const body: unknown = await response.json();
  if (!response.ok) {
    const error = body as { message?: unknown; detail?: unknown };
    const detail = error.detail && typeof error.detail === 'object' ? error.detail as { message?: unknown } : null;
    throw new ChatForkError(
      typeof detail?.message === 'string' ? detail.message
        : typeof error.message === 'string' ? error.message
        : typeof error.detail === 'string' ? error.detail : `HTTP ${response.status}`,
      response.status,
    );
  }
  const session = unwrapData<ChatForkSession>(body);
  if (!session?.chat_id || !session.metadata?.fork) throw new Error('Invalid chat fork response');
  return session;
}
