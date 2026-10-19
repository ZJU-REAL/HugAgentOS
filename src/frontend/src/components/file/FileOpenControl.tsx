import { useCallback, useEffect, useRef, useState } from 'react';
import { Dropdown } from 'antd';
import { DownOutlined, ExportOutlined, FolderOpenOutlined } from '@ant-design/icons';
import { authFetch, unwrapData } from '../../api';
import { artifactUrl } from '../../utils/artifactAccess';
import './FileOpenControl.css';
import type { ArtifactLocation } from '../../utils/artifactAccess';
import { t } from '../../i18n';


export interface FileActionPhase {
  kind: 'pending' | 'success' | 'error';
  message: string;
}

interface Application {
  id: string;
  name: string;
  icon: string | null;
}

interface Applications {
  applications: Application[];
  default_id: string | null;
  path?: string;
}

async function readResponse<Result>(response: Response): Promise<Result> {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || payload.message || t('无法打开文件'));
  return unwrapData<Result>(payload);
}

const ignorePath = () => {};
const ignorePhase = () => {};

function ApplicationIcon({ source, size }: { source: string | null; size: number }) {
  const [failed, setFailed] = useState(false);
  return source && !failed ? <img src={source} alt="" width={size} height={size} onError={() => setFailed(true)} />
    : <ExportOutlined aria-hidden="true" />;
}

export function FileOpenControl({ file, onPhase = ignorePhase, onPath = ignorePath, disabled = false }: {
  file: ArtifactLocation & { file_id: string; name?: string };
  disabled?: boolean;
  onPhase?: (phase: FileActionPhase | undefined) => void;
  onPath?: (path: string) => void;
}) {
  const [apps, setApps] = useState<Applications>();
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const inFlight = useRef(false);
  const mounted = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const actionUrl = artifactUrl({ ...file, url: '/files/' + encodeURIComponent(file.file_id) });
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const url = new URL(actionUrl, window.location.href);
      url.pathname += '/applications';
      const response = await authFetch(url.toString(), { signal });
      const result = await readResponse<Applications>(response);
      if (!Array.isArray(result.applications) || result.applications.some(app =>
        typeof app.id !== 'string' || !app.id || typeof app.name !== 'string')) {
        throw new Error(t('重新读取关联应用'));
      }
      if (signal?.aborted || !mounted.current) return;
      setApps(result);
      setFailed(false);
      if (result.path) onPath(result.path);
    } catch {
      if (!signal?.aborted && mounted.current) setFailed(true);
    }
  }, [actionUrl, onPath]);
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void load(controller.signal);
    return () => { mounted.current = false; controller.abort(); };
  }, [load]);
  useEffect(() => () => clearTimeout(timer.current), []);
  const preferred = apps?.applications.find(app => app.id === apps.default_id) || apps?.applications[0];
  const revealDefault = apps !== undefined && preferred === undefined;
  const execute = async (action: 'open' | 'reveal', application?: string) => {
    if (inFlight.current || disabled) return;
    inFlight.current = true;
    clearTimeout(timer.current);
    setMenuOpen(false);
    setBusy(true);
    onPhase({ kind: 'pending', message: t(action === 'reveal' ? '正在显示文件…' : '正在打开文件…') });
    try {
      const url = new URL(actionUrl, window.location.href);
      url.pathname += '/open';
      const response = await authFetch(url.toString(), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, application }),
      });
      await readResponse(response);
      if (!mounted.current) return;
      onPhase({ kind: 'success', message: t(action === 'reveal' ? '已显示文件' : '已打开文件') });
      timer.current = setTimeout(() => onPhase(undefined), 5200);
    } catch (error) {
      if (mounted.current) onPhase({ kind: 'error', message: error instanceof Error ? error.message : t('无法打开文件') });
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  const label = revealDefault ? t('打开所在文件夹') : preferred
    ? t('使用 {app} 打开', { app: preferred.name }) : t('打开文件');
  const items = [
    ...(apps?.applications || []).map(app => ({
      key: 'app:' + app.id,
      label: app.id === preferred?.id ? t('{app}（默认）', { app: app.name }) : app.name,
      icon: <ApplicationIcon key={app.icon} source={app.icon} size={16} />,
      onClick: () => { void execute('open', app.id); },
    })),
    ...(failed ? [{ key: 'retry', label: t('重新读取关联应用'), onClick: () => { void load(); } }] : []),
    ...(apps?.applications.length ? [{ key: 'divider', type: 'divider' as const }] : []),
    { key: 'reveal', label: t('打开所在文件夹'), icon: <FolderOpenOutlined aria-hidden="true" />,
      onClick: () => { void execute('reveal'); } },
  ];
  return <div className="hg-delivery-split" data-state={busy ? 'busy' : 'idle'}>
    <button type="button" className="hg-delivery-open" disabled={disabled || busy || (!apps && !failed)}
      aria-label={label} title={label}
      onClick={() => { void execute(revealDefault ? 'reveal' : 'open',
        preferred && preferred.id !== apps?.default_id ? preferred.id : undefined); }}>
      {!apps && !failed ? <span className="hg-delivery-skeleton" aria-hidden="true" />
        : revealDefault ? <FolderOpenOutlined /> : <ApplicationIcon key={preferred?.icon} source={preferred?.icon || null} size={13} />}
    </button>
    <Dropdown trigger={['click']} open={menuOpen && !busy && !disabled} onOpenChange={open => { setMenuOpen(open); if (open) void load(); }} menu={{ items }} disabled={busy || disabled || (!apps && !failed)}>
      <button type="button" className="hg-delivery-more" disabled={busy || disabled || (!apps && !failed)}
        aria-label={t('打开选项')} aria-haspopup="menu" aria-expanded={menuOpen && !busy && !disabled}>
        <DownOutlined />
      </button>
    </Dropdown>
  </div>;
}
