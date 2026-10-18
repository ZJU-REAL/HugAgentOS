import { useCallback, useEffect, useRef, useState } from 'react';
import { message } from 'antd';
import { listSpaceEntries, listSpaceRoots, type SpaceEntry, type SpacePage, type SpaceRoot } from './spaceFilesApi';

export function useSpaceDirectory(kind: 'personal' | 'team') {
  const [roots, setRoots] = useState<SpaceRoot[]>([]);
  const [selected, setSelected] = useState('');
  const [path, setPath] = useState('');
  const [page, setPage] = useState<SpacePage | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [dialog, setDialog] = useState<SpaceEntry | 'directory' | null>(null);
  const [name, setName] = useState('');

  const [permissions, setPermissions] = useState(false);

  const generation = useRef(0);
  const root = roots.find(item => item.space_id === selected);
  const writable = root && root.permission !== 'view';
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
    });
    return () => abort.abort();
  }, [kind, revision]);

  useEffect(() => {
    setPath('');
    setPage(null);
  }, [selected]);

  useEffect(() => {
    const abort = new AbortController();
    const current = ++generation.current;
    setPage(null);
    if (!selected) return () => abort.abort();
    setLoading(true);
    void listSpaceEntries(selected, path, undefined, abort.signal).then(result => {
      if (generation.current !== current) return;
      setPage(result);
      setRoots(items => items.map(item => item.space_id === selected ? result.space : item));
      setError('');
    }).catch(reason => {
      if (!abort.signal.aborted) setError(String(reason));
    }).finally(() => {
      if (!abort.signal.aborted) setLoading(false);
    });
    return () => abort.abort();
  }, [selected, path, revision]);

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
    if (!page?.next_cursor) return;
    const current = generation.current;
    setLoading(true);
    try {
      const next = await listSpaceEntries(selected, path, page.next_cursor);
      if (generation.current === current) {
        setPage(value => value && { ...next, entries: [...value.entries, ...next.entries] });
      }
    } catch (reason) {
      if (generation.current === current) setError(String(reason));
    } finally {
      if (generation.current === current) setLoading(false);
    }
  }
  return { revision, roots, selected, setSelected, path, setPath, page, loading, busy, error, root,
    writable, reload, perform, loadMore, dialog, setDialog, name, setName, permissions, setPermissions };
}
