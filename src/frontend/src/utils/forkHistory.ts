import type { ChatMessage } from '../types';

/** Persisted display-only provenance; source tasks never become active in a fork. */
export interface ForkedHistoryMessage extends ChatMessage {
  forkedHistory?: boolean;
  /** Display-only original provider usage; never billed for the copied turn. */
  usage?: Record<string, unknown>;
}

export function isForkedHistory(message: ChatMessage): boolean {
  return (message as ForkedHistoryMessage).forkedHistory === true;
}
