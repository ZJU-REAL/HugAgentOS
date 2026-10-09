import { authFetch, chatTargetHeaders, getApiUrl } from './api';

/** The ordinary-chat state subscription starts with an aligned native snapshot. */
export function openRunSubscription(
  runId: string,
  signal?: AbortSignal,
  chatId?: string,
): Promise<Response> {
  return authFetch(
    `${getApiUrl()}/v1/chats/stream/${encodeURIComponent(runId)}/subscription`,
    { method: 'GET', signal, headers: chatTargetHeaders(chatId) },
  );
}
