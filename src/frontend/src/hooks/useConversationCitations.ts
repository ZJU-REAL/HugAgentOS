import { useMemo } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useChatStore } from '../stores/chatStore';
import { citationMarkerParts, citationMarkerRe } from '../utils/citations';
import { resolveIndexedCitations } from '../utils/conversationCitations';
import type { ChatMessage, CitationItem } from '../types';
const EMPTY: CitationItem[] = [];

export function useConversationCitations(chatId: string, message: ChatMessage, position: number) {
  const own = message.citations ?? EMPTY;
  const ids = useMemo(() => {
    const text = [message.content, ...(message.segments ?? []).filter(s => s.type === 'text').map(s => s.content ?? '')].join('\n');
    return [...new Set(Array.from(text.matchAll(citationMarkerRe()), m => citationMarkerParts(m).id))];
  }, [message.content, message.segments]);
  return useChatStore(useShallow(state => resolveIndexedCitations(
    state.store.chats[chatId]?.messages ?? [], position, own, ids,
  )));
}
