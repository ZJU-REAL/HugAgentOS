/**
 * Custom hook template.
 *
 * Replace ${Feature}, ${feature} with actual names.
 * Create as hooks/use${Feature}.ts. hooks/index.ts only re-exports the most common hooks;
 * import others by deep path.
 *
 * Rules:
 *   - Network calls go through API functions (api.ts / src/api/<domain>.ts), which use apiRequest()
 *   - Every request started by the hook is aborted on unmount or when superseded
 *   - Effects are split by concern; no conditional hooks
 */

import { useCallback, useEffect, useRef } from 'react';
import { use${Feature}Store } from '../stores';

export function use${Feature}() {
  const fetchItems = use${Feature}Store((s) => s.fetchItems);
  const loading = use${Feature}Store((s) => s.loading);
  const abortRef = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    await fetchItems(controller.signal);
  }, [fetchItems]);

  // Initial load + cleanup.
  useEffect(() => {
    void refresh();
    return () => abortRef.current?.abort();
  }, [refresh]);

  return { loading, refresh };
}
