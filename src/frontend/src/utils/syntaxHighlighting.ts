import hljs from 'highlight.js/lib/common';
import { useSyncExternalStore } from 'react';

let revision = 0;
let loading: Promise<void> | undefined;
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};
const snapshot = () => revision;

/** Common grammars are immediate; uncommon grammars never delay readable code. */
export function requestLanguage(language: string): void {
  if (!language || hljs.getLanguage(language) || revision > 0 || loading) return;
  loading = import('highlight.js').then(() => {
    revision += 1;
    listeners.forEach(listener => listener());
  }).catch(() => { loading = undefined; });
}

export function useSyntaxHighlighting(): number {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

export default hljs;
