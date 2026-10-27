import { useCallback, useEffect, useRef, useState } from 'react';
import { message } from 'antd';
import { listSpaceEntries, listSpaceRoots, type SpaceEntry, type SpacePage, type SpaceRoot } from './spaceFilesApi';

export function useSpaceDirectory(kind: 'personal' | 'team', query = '') {
  const keyword = query.trim();
  const [roots, setRoots] = useState<SpaceRoot[]>([]);
  const [selected, setSelected] = useState('');
  const [path, setPath] = useState('');
  const [page, setPage] = useState<SpacePage | null>(null);
  const [loading, setLoading] = useState(false);
  const [rootsLoaded, setRootsLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [dialog, setDialog] = useState<SpaceEntry | 'directory' | null>(null);
  const [name, setName] = useState('');

  const [permissions, setPermissions] = useState(false);

  const generation = useRef(0);
  const paginationAbort = useRef<AbortController | null>(null);
  const root = roots.find(item => item.space_id === selected);
  const writable = root && root.permission !== 'view';
  // True until the first listing of the current space arrives, so the view shows a skeleton instead of flashing empty.
  const initializing = !error && (!rootsLoaded || (!!selected && !page));
  const reload = useCallback(() => setRevision(value => value + 1), []);

  useEffect(() => {
    const refresh = () => reload();
    window.addEventListener('focus', refresh);
    return () => window.removeEventListener('focus', refresh);
  }, [reload]);

  useEffect(() => {
    const abort = new AbortController();
    setError('');
    void listSpaceRoots(abort.signal).then(result => {
      const visible = result.items.filter(item => item.kind === kind);
      setRoots(visible);
      setSelected(value => visible.some(item => item.space_id === value) ? value : visible[0]?.space_id ?? '');
    }).catch(reason => {
      if (!abort.signal.aborted) {
        setRoots([]);
        setSelected('');
        setError(String(reason));
      }
    }).finally(() => {
      if (!abort.signal.aborted) setRootsLoaded(true);
    });
    return () => abort.abort();
  }, [kind, revision]);

  // Switching space normally starts at its root; open() can hand over a target path for the new space.
  const pendingPath = useRef<string | null>(null);
  useEffect(() => {
    setPath(pendingPath.current ?? '');
    pendingPath.current = null;
    setPage(null);
  }, [selected]);
  const open = useCallback((space: string, next: string) => {
    if (space === selected) { setPath(next); return; }
    pendingPath.current = next;
    setSelected(space);
  }, [selected]);

  useEffect(() => {
    const abort = new AbortController();
    const current = ++generation.current;
    setPage(null);
    setError('');
    if (!selected) { setLoading(false); return () => abort.abort(); }
    setLoading(true);
    const timer = setTimeout(() => {
      void listSpaceEntries(selected, path, undefined, abort.signal, keyword).then(result => {
        if (generation.current !== current) return;
        setPage(result);
        setRoots(items => items.map(item => item.space_id === selected ? result.space : item));
        setError('');
      }).catch(reason => {
        if (!abort.signal.aborted) setError(String(reason));
      }).finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    }, keyword ? 250 : 0);
    return () => {
      clearTimeout(timer);
      abort.abort();
      paginationAbort.current?.abort();
    };
  }, [selected, path, revision, keyword]);

  async function perform(action: () => Promise<unknown>) {
    setBusy(true);
    try {
      await action();
      setDialog(null);
    } catch (reason) {
      void message.error(String(reason));
    } finally {
      setBusy(false);
      reload();
    }
  }
  async function loadMore() {
    if (loading || !page?.next_cursor) return;
    const current = generation.current;
    const abort = new AbortController();
    paginationAbort.current = abort;
    setLoading(true);
    try {
      const next = await listSpaceEntries(selected, path, page.next_cursor, abort.signal, keyword);
      if (!abort.signal.aborted && generation.current === current) {
        setPage(value => value && { ...next, entries: [...value.entries, ...next.entries] });
      }
    } catch (reason) {
      if (!abort.signal.aborted && generation.current === current) setError(String(reason));
    } finally {
      if (!abort.signal.aborted && generation.current === current) setLoading(false);
    }
  }
  return { revision, roots, selected, setSelected, open, path, setPath, page, loading, initializing, busy, error, root,
    writable, reload, perform, loadMore, dialog, setDialog, name, setName, permissions, setPermissions };
}
