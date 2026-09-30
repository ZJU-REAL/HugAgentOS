import type { ChatMessage } from '../types';
import { hasUnclosedThink } from './segments';

/** A durable message offset does not preserve an inline reasoning parser's phase.
 * Thinking runs replay from the beginning through the ordinary stream parser.
 */
export function streamResumeSeed(
  message: ChatMessage | undefined,
  enableThinking: boolean,
): ChatMessage | undefined {
  if (!message || enableThinking || hasUnclosedThink(message.content)) return undefined;
  return message;
}
