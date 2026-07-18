/**
 * Custom hook template.
 *
 * Replace ${Feature}, ${feature} with actual names.
 * Create as hooks/use${Feature}.ts
 */

import { useState, useEffect, useCallback, useRef } from 'react';
// import { use${Feature}Store } from '../stores';
// import { authFetch } from '../api';

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function use${Feature}(apiUrl: string) {
  // --- Store state ---
  // const { items, setItems, loading, setLoading } = use${Feature}Store();

  // --- Local state ---
  const [initialized, setInitialized] = useState(false);

  // --- Refs ---
  const abortRef = useRef<AbortController | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // --- Initialization ---
  useEffect(() => {
    if (initialized) return;

    const init = async () => {
      try {
        // const r = await authFetch(`${apiUrl}/v1/${feature}s`);
        // const { data } = await r.json();
        // setItems(data.items);
        setInitialized(true);
      } catch (e) {
        console.error('Failed to init ${feature}:', e);
      }
    };

    init();
  }, [apiUrl, initialized]);

  // --- Cleanup ---
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  // --- Actions ---
  const refresh = useCallback(async () => {
    // setLoading(true);
    try {
      // const r = await authFetch(`${apiUrl}/v1/${feature}s`);
      // const { data } = await r.json();
      // setItems(data.items);
    } catch (e) {
      console.error('Refresh failed:', e);
    } finally {
      // setLoading(false);
    }
  }, [apiUrl]);

  const doSomething = useCallback(async (id: string) => {
    abortRef.current = new AbortController();
    try {
      // await authFetch(`${apiUrl}/v1/${feature}s/${id}/action`, {
      //   method: 'POST',
      //   signal: abortRef.current.signal,
      // });
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        console.error('Action failed:', e);
      }
    }
  }, [apiUrl]);

  // --- Return ---
  return {
    initialized,
    refresh,
    doSomething,
    abortRef,
  };
}
