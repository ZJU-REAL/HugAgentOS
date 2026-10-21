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
const applicationsLifetime = 30_000;

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
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const inFlight = useRef(false);
  const generation = useRef(0);
  const discovery = useRef<AbortController | undefined>(undefined);
  const iconDiscovery = useRef<AbortController | undefined>(undefined);
  const loadedAt = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const actionUrl = artifactUrl({ ...file, url: '/files/' + encodeURIComponent(file.file_id) });
  useEffect(() => {
    const current = ++generation.current;
    loadedAt.current = 0;
    inFlight.current = false;
    setApps(undefined);
    setFailed(false);
    setLoading(false);
    setBusy(false);
    setMenuOpen(false);
    return () => {
      generation.current = current + 1;
      discovery.current?.abort();
      discovery.current = undefined;
      iconDiscovery.current?.abort();
      iconDiscovery.current = undefined;
      clearTimeout(timer.current);
      onPhase(undefined);
    };
  }, [actionUrl, onPhase]);
  const load = useCallback(async () => {
    if (discovery.current || Date.now() - loadedAt.current < applicationsLifetime) return;
    iconDiscovery.current?.abort();
    iconDiscovery.current = undefined;
    const controller = new AbortController();
    discovery.current = controller;
    setLoading(true);
    setFailed(false);
    try {
      const url = new URL(actionUrl, window.location.href);
      url.pathname += '/applications';
      url.searchParams.set('include_icons', 'false');
      const response = await authFetch(url.toString(), { signal: controller.signal });
      const result = await readResponse<Applications>(response);
      if (!Array.isArray(result.applications) || result.applications.some(app =>
        typeof app.id !== 'string' || !app.id || typeof app.name !== 'string')) {
        throw new Error(t('重新读取关联应用'));
      }
      if (controller.signal.aborted) return;
      setApps(result);
      loadedAt.current = Date.now();
      if (result.path) onPath(result.path);
      setLoading(false);
      discovery.current = undefined;
      if (!result.applications.length) return;
      iconDiscovery.current = controller;
      // Icons are decorative. Never delay usable choices or actions for native image decoding.
      url.searchParams.set('include_icons', 'true');
      try {
        const iconsResponse = await authFetch(url.toString(), { signal: controller.signal });
        const icons = await readResponse<Applications>(iconsResponse);
        if (controller.signal.aborted || !Array.isArray(icons.applications)) return;
        const byId = new Map(icons.applications.map(app => [app.id, app.icon]));
        setApps(current => current && { ...current, applications: current.applications.map(app =>
          ({ ...app, icon: byId.get(app.id) || app.icon })) });
      } catch {
        // Keep working names and fallback icons when optional native icons are unavailable.
      }
    } catch {
      if (!controller.signal.aborted) setFailed(true);
    } finally {
      if (iconDiscovery.current === controller) iconDiscovery.current = undefined;
      if (discovery.current === controller) {
        discovery.current = undefined;
        setLoading(false);
      }
    }
  }, [actionUrl, onPath]);
  const preferred = apps?.applications.find(app => app.id === apps.default_id);
  const execute = async (action: 'open' | 'reveal', application?: string) => {
    if (inFlight.current || disabled) return;
    const current = generation.current;
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
      if (generation.current !== current) return;
      onPhase({ kind: 'success', message: t(action === 'reveal' ? '已显示文件' : '已打开文件') });
      timer.current = setTimeout(() => onPhase(undefined), 5200);
    } catch (error) {
      if (generation.current === current) {
        onPhase({ kind: 'error', message: error instanceof Error ? error.message : t('无法打开文件') });
        // An application may have been removed since discovery; retry must query the OS again.
        loadedAt.current = 0;
        discovery.current?.abort();
        discovery.current = undefined;
        iconDiscovery.current?.abort();
        iconDiscovery.current = undefined;
        setLoading(false);
      }
    } finally {
      if (generation.current === current) {
        inFlight.current = false;
        setBusy(false);
      }
    }
  };
  const label = preferred ? t('使用 {app} 打开', { app: preferred.name }) : t('打开文件');
  const items = [
    { key: 'reveal', label: t('打开所在文件夹'), icon: <FolderOpenOutlined aria-hidden="true" />,
      onClick: () => { void execute('reveal'); } },
    ...(loading || failed || apps?.applications.length ? [{ key: 'divider', type: 'divider' as const }] : []),
    ...(loading ? [{ key: 'loading', label: t('正在读取关联应用…'), disabled: true }] : []),
    ...(failed ? [{ key: 'retry', label: t('重新读取关联应用'), onClick: () => { void load(); } }] : []),
    ...(apps?.applications || []).map(app => ({
      key: 'app:' + app.id,
      label: app.id === apps?.default_id ? t('{app}（默认）', { app: app.name }) : app.name,
      icon: <ApplicationIcon key={app.icon} source={app.icon} size={16} />,
      onClick: () => { void execute('open', app.id); },
    })),
  ];
  return <div className="hg-delivery-split" data-state={busy ? 'busy' : 'idle'}>
    <button type="button" className="hg-delivery-open" disabled={disabled || busy}
      aria-label={label} title={label} onClick={() => { void execute('open'); }}>
      <ApplicationIcon key={preferred?.icon} source={preferred?.icon || null} size={13} />
    </button>
    <button type="button" className="hg-delivery-folder" disabled={disabled || busy}
      aria-label={t('打开所在文件夹')} title={t('打开所在文件夹')}
      onClick={() => { void execute('reveal'); }}><FolderOpenOutlined /></button>
    <Dropdown trigger={['click']} placement="bottomRight" classNames={{ root: 'hg-delivery-app-menu' }}
      open={menuOpen && !busy && !disabled} onOpenChange={(open, info) => {
        if (!open && info.source === 'menu') return;
        setMenuOpen(open);
        if (open) void load();
      }}
      menu={{ items }} disabled={busy || disabled}>
      <button type="button" className="hg-delivery-more" disabled={busy || disabled}
        aria-label={t('打开选项')} aria-haspopup="menu" aria-expanded={menuOpen && !busy && !disabled}>
        <DownOutlined />
      </button>
    </Dropdown>
  </div>;
}
