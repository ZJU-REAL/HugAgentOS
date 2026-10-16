export interface StandaloneTarget {
  kind: 'agents' | 'projects';
  resourceId: string;
  chatId?: string;
}

export function standaloneFromPath(pathname = window.location.pathname): StandaloneTarget | null {
  const match = /^\/chat\/(agents|projects)\/([^/]+)(?:\/c\/([^/]+))?\/?$/.exec(pathname);
  if (!match) return null;
  try {
    const resourceId = decodeURIComponent(match[2]);
    const chatId = match[3] ? decodeURIComponent(match[3]) : undefined;
    if ([resourceId, chatId].some(value => value && (value.includes('/') || value.includes('\\') || [...value].some(char => char.charCodeAt(0) < 32)))) return null;
    return { kind: match[1] as StandaloneTarget['kind'], resourceId, ...(chatId ? { chatId } : {}) };
  } catch { return null; }
}

export function pathForStandalone(target: StandaloneTarget, chatId?: string | null): string {
  const base = `/chat/${target.kind}/${encodeURIComponent(target.resourceId)}`;
  return chatId ? `${base}/c/${encodeURIComponent(chatId)}` : base;
}
