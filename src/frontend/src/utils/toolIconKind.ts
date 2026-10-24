/**
 * The line icons in components/tool/toolIcons.tsx: one per kind of action, so
 * every shell entry point shares the terminal and the knowledge-base
 * retrievers share the book. Which tool shows which icon lives next to its
 * label in utils/toolActionLabel.ts (`toolIconKind`).
 */
export type ToolIconKind =
  | 'terminal' | 'fileRead' | 'fileWrite' | 'edit' | 'findFiles' | 'grep' | 'image'
  | 'webSearch' | 'globe' | 'knowledge' | 'graph' | 'database'
  | 'skill' | 'plugin' | 'catalog' | 'agent' | 'ask' | 'job'
  | 'folderUp' | 'folderDown' | 'pin' | 'word' | 'table' | 'publish'
  | 'chats' | 'notes' | 'compress' | 'thinking' | 'wrench';
