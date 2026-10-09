import { useEffect, useState } from 'react';
import { applicationRequest, type RecordRow, type Target } from './applicationApi';
import { useApplications } from './useApplications';

/** Share owner-list loading with cards; key results so old selections never render stale rows. */
export function useApplicationData(siteId: string | undefined, target: Target, applicationId?: string) {
  const source = useApplications(target);
  const apps = source.apps.filter((app) =>
    (!siteId || app.site_id === siteId) && (!applicationId || app.id === applicationId),
  );
  const [requestedAppId, setAppId] = useState('');
  const [requestedTable, setTableName] = useState('');
  const [pagination, setPagination] = useState({ key: '', page: 1 });
  const [result, setResult] = useState<{ key: string; records: RecordRow[]; total: number; error: string }>({
    key: '', records: [], total: 0, error: '',
  });
  const selected = apps.find((app) => app.id === requestedAppId) || apps[0];
  const appId = selected?.id || '';
  const tableKeys = Object.keys(selected?.tables || {});
  const tableName = tableKeys.includes(requestedTable) ? requestedTable : tableKeys[0] || '';
  const table = selected?.tables[tableName];
  const selection = `${target}:${appId}:${tableName}`;
  const page = pagination.key === selection ? pagination.page : 1;
  const setPage = (next: number) => setPagination({ key: selection, page: next });
  const queryKey = `${selection}:${page}:${source.revision}`;

  useEffect(() => {
    if (!appId || !tableName) return;
    const controller = new AbortController();
    applicationRequest<{ items: RecordRow[]; total: number }>(
      `/v1/applications/${appId}/tables/${tableName}/records?limit=20&offset=${(page - 1) * 20}`,
      { signal: controller.signal }, target,
    ).then((response) => {
      if (!controller.signal.aborted) setResult({ key: queryKey, records: response.items, total: response.total, error: '' });
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setResult({ key: queryKey, records: [], total: 0,
        error: failure instanceof Error ? failure.message : String(failure) });
    });
    return () => controller.abort();
  }, [appId, tableName, page, queryKey, target]);

  const current = result.key === queryKey;
  return { available: source.available, apps, appId, setAppId, tableName, setTableName, selected, table,
    records: current ? result.records : [], total: current ? result.total : 0, page, setPage,
    loading: source.loading || (!!appId && !!tableName && !current),
    error: source.error || (current ? result.error : ''), reload: source.reload };
}
