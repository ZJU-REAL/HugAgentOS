import { createContext, useContext } from 'react';
export interface PublicChatInfo {
  id: string;
  kind: 'agents' | 'projects';
  name: string;
  description: string;
  suggested_questions: string[];
  visitor_id: string;
  chats: Record<string, unknown>[];
}
export const PublicChatContext = createContext<PublicChatInfo | null>(null);
export const usePublicChat = () => useContext(PublicChatContext);
