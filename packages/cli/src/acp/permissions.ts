import type { AgentContext } from '@agentclientprotocol/sdk';
import type { ChatEvent } from '../engine/engine.js';
import type { Session } from './sessions.js';

/** A cancelled peer request must not keep the prompt waiting for an eventual client response. */
export function abortable<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => reject(new DOMException('Cancelled', 'AbortError'));
    if (signal.aborted) { work.catch(() => {}); abort(); return; }
    signal.addEventListener('abort', abort, { once: true });
    work.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
  });
}
export async function permission(s: Session, e: ChatEvent, client: AgentContext, signal: AbortSignal): Promise<void> {
  if (typeof e.confirm_id !== 'string' || e.expired) return;
  const response = await abortable(client.request('session/request_permission', {
    sessionId: s.id,
    toolCall: { toolCallId: String(e.tool_call_id ?? e.tool_id ?? e.confirm_id),
      title: String(e.title ?? e.description ?? e.path ?? e.command ?? 'Approve tool action'), status: 'pending' },
    options: [{ optionId: 'allow', name: 'Allow once', kind: 'allow_once' },
      { optionId: 'deny', name: 'Reject once', kind: 'reject_once' }],
  }, { cancellationSignal: signal }), signal);
  if (signal.aborted) return;
  await s.engine.confirm(s.id, e.confirm_id,
    response.outcome.outcome === 'selected' && response.outcome.optionId === 'allow' ? 'allow' : 'deny');
}
