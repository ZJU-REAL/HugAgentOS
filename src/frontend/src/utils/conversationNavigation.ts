import type { ChatMessage } from '../types';
import { stripMarkdown } from './markdown';

export interface ConversationTurn {
  uid: string;
  title: string;
  preview: string;
}

function previewText(message: ChatMessage): string {
  const text = message.segments?.filter((segment) => segment.type === 'text')
    .map((segment) => segment.content || '').join('\n') || message.content;
  return stripMarkdown(text.replace(/<think>[\s\S]*?(?:<\/think>|$)/gi, '').slice(0, 4000)).slice(0, 280);
}

/** Preserve display order and anchor to the user message's identity, including after history prepends. */
export function buildConversationTurns(messages: readonly ChatMessage[]): ConversationTurn[] {
  const turns: ConversationTurn[] = [];
  for (const message of messages) {
    if (message.role === 'user') {
      turns.push({
        uid: message.uid,
        title: previewText(message) || message.attachments?.map((file) => file.name).join(', ') || '',
        preview: '',
      });
    } else if (message.role === 'assistant') {
      const turn = turns[turns.length - 1];
      if (turn && !turn.preview) turn.preview = previewText(message);
    }
  }
  return turns;
}
