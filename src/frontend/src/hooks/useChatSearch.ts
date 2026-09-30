import { useEffect, useRef } from 'react';
import { searchSessions } from '../api';
import { useUIStore } from '../stores';
export function useChatSearch() {
  const { searchKeyword, setSearchResults, setSearchLoading } = useUIStore();
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // ── Search debounce ──
  // Use searchSessions (which fully resolves mode fields like agentName/planChat via
  // toChatItem); otherwise SearchModal's _mode:* type filters would all fail on search hits.
  // The cancelled flag prevents a late fetch from backfilling stale results after the user
  // clears/closes the search.
  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    const kw = searchKeyword.trim();
    if (!kw) {
      setSearchResults([]);
      setSearchLoading(false);
      return;
    }
    setSearchLoading(true);
    let cancelled = false;
    searchTimerRef.current = setTimeout(async () => {
      try {
        const { items } = await searchSessions(kw, 1, 50);
        if (cancelled) return;
        setSearchResults(items);
      } catch {
        if (!cancelled) setSearchResults([]);
      } finally {
        if (!cancelled) setSearchLoading(false);
      }
    }, 300);
    return () => {
      cancelled = true;
      if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    };
  }, [searchKeyword, setSearchResults, setSearchLoading]);

}
