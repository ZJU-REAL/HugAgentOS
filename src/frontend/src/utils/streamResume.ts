import type { ChatMessage } from '../types';

/** History supplies identity only; the ordered log supplies the aligned state. */
export function streamResumeSeed(message: ChatMessage | undefined): ChatMessage | undefined {
  return message ? { uid: message.uid, messageId: message.messageId, role: 'assistant',
    content: '', ts: message.ts } : undefined;
}
