import { useCallback, useEffect, useRef, useState } from 'react';
import { message } from 'antd';
import type { ApiKeyItem } from '../../api';
import { t } from '../../i18n';

/** All reads and mutations belong to one mounted resource dialog. Closing it cancels
 * requests and suppresses late plaintext, fallback clipboard writes and state updates. */
export function useApiKeys<T extends ApiKeyItem>(resourceId: string, listKeys: (id: string, signal?: AbortSignal) => Promise<T[]>) {
  const [keys, setKeys] = useState<T[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [pending, setPending] = useState<Set<string>>(new Set());
  const requests = useRef(new Set<AbortController>());
  const locks = useRef(new Set<string>());
  const mounted = useRef(false);
  const revision = useRef(0);
  const reload = useCallback(async () => {
    const version = ++revision.current;
    const controller = new AbortController();
    requests.current.add(controller);
    setLoading(true);
    setError('');
    try {
      const items = await listKeys(resourceId, controller.signal);
      if (mounted.current && version === revision.current) setKeys(items);
    } catch (e) {
      if (!controller.signal.aborted && mounted.current && version === revision.current) setError((e as Error).message);
    } finally {
      requests.current.delete(controller);
      if (mounted.current && version === revision.current) setLoading(false);
    }
  }, [resourceId, listKeys]);

  useEffect(() => {
    mounted.current = true;
    void reload();
    const activeRequests = requests.current;
    return () => {
      mounted.current = false;
      for (const request of activeRequests) request.abort();
      activeRequests.clear();
    };
  }, [reload]);

  const perform = async <T,>(
    id: string, action: (signal: AbortSignal) => Promise<T>, onSuccess: (value: T, signal: AbortSignal) => void | Promise<void>,
  ) => {
    if (locks.current.has(id)) return;
    locks.current.add(id);
    setPending(new Set(locks.current));
    const controller = new AbortController();
    requests.current.add(controller);
    try {
      const value = await action(controller.signal);
      if (mounted.current && !controller.signal.aborted) await onSuccess(value, controller.signal);
    } catch (e) {
      if (mounted.current && !controller.signal.aborted) {
        message.error(t('操作失败：{msg}', { msg: (e as Error).message }));
      }
    } finally {
      requests.current.delete(controller);
      locks.current.delete(id);
      if (mounted.current) setPending(new Set(locks.current));
    }
  };
  return { keys, loading, error, pending, reload, perform };
}
