import { sseData } from '@hugagent/chat-sdk';
import { handleSsePayload } from './payload';
import type { ChatStreamState } from './state';

export function processSseBlock(s: ChatStreamState, block: string) {
  const data = sseData(block);
  if (data !== null) handleSsePayload(s, data);
}
