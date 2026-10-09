import { useCallback, useEffect, useRef, useState } from 'react';
import { applicationRequest, type Application, type Target } from './applicationApi';

/** Load owner applications without selecting a table or fetching record data. */
export function useApplications(target: Target = 'cloud') {
  const [apps, setApps] = useState<Application[]>([]);
  const [available, setAvailable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const pending = useRef<AbortController | null>(null);
  const reload = useCallback(async () => {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true);
    setError('');
    try {
      const result = await applicationRequest<{ items: Application[]; available: boolean }>(
        '/v1/applications', { signal: controller.signal }, target,
      );
      if (!controller.signal.aborted) {
        setApps(result.items);
        setAvailable(result.available);
        setRevision((value) => value + 1);
      }
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [target]);
  useEffect(() => {
    setApps([]);
    void reload();
    return () => pending.current?.abort();
  }, [reload]);
  return { apps, available, loading, error, revision, reload };
}
