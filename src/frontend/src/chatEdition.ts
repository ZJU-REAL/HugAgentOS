import type { ChatDetail } from './api';

export type ChatAccessLevel = 'admin' | 'edit' | 'read';

export function chatAccessLevel(_detail: ChatDetail): ChatAccessLevel | null {
  return null;
}
