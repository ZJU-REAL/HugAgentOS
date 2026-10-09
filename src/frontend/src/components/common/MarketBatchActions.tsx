import type { ReactNode } from 'react';
type Options = { open: boolean; resetKey?: string; items: { slug: string; name: string; deletable: boolean }[]; kind: 'skill' | 'agent' | 'plugin'; token?: string; onDeleted: () => Promise<void> };
export function useMarketSelection(options: Options) {
  void options;
  return { checkbox: (slug: string, name: string): ReactNode => { void slug; void name; return null; } };
}
