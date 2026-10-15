import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowLeftOutlined, DownloadOutlined, FolderOutlined, LockOutlined, ReloadOutlined } from '@ant-design/icons';
import { authFetch, unwrapData } from '../../api';
import { t } from '../../i18n';
import type { ResourceItem } from '../../types';
import { artifactOrigin, artifactUrl, type ArtifactLocation } from '../../utils/artifactAccess';
import { archiveChildren, archiveError, archiveUrl, type ArchiveEntry, type ArchiveListing } from '../../utils/archiveBrowser';
import { formatPreviewBytes } from '../../utils/filePreviewSafety';
import { getFileIconSrc } from '../../utils/fileIcon';
import './ArchiveBrowser.css';

interface Props {
  file: ArtifactLocation & { name: string; size?: number };
  renderMember: (item: ResourceItem) => ReactNode;
}

export function ArchiveBrowser({ file, renderMember }: Props) {
  const url = archiveUrl(file);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ url: string; data?: ArchiveListing; error?: string }>({ url: '' });
  const [folder, setFolder] = useState('');
  const [selected, setSelected] = useState<ArchiveEntry | null>(null);
  const [search, setSearch] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    async function load() {
      try {
        if (!url) throw new Error(t('此文件无法在线浏览，请下载后解压'));
        const response = await authFetch(url, { signal: controller.signal });
        const payload = await response.json();
        if (!response.ok) throw new Error(
          response.status === 401 || response.status === 403 || response.status === 404
            ? t('文件不存在或无权访问') : archiveError(payload.detail),
        );
        const data = unwrapData<ArchiveListing>(payload);
        if (active) setState({ url, data });
      } catch (error) {
        if (active) setState({ url, error: error instanceof Error ? error.message : archiveError(null) });
      }
    }
    void load();
    return () => { active = false; controller.abort(); };
  }, [url, attempt]);

  const retry = () => { setState({ url: '' }); setAttempt(value => value + 1); };
  const downloadOriginal = <a href={artifactUrl(file)} download={file.name}>{t('下载原压缩包')}</a>;
  if (state.url !== url) return <div className="jx-archive-status" aria-busy="true"><div className="jx-canvas-spinner" />{t('正在读取压缩包…')}</div>;
  if (state.error) return <div className="jx-archive-status" role="alert">
    <span>{state.error}</span>
    <button type="button" aria-label={t('重试')} onClick={retry}><ReloadOutlined />{t('重试')}</button>
    {downloadOriginal}
  </div>;
  if (!state.data) return null;

  const navigate = (next: string) => { setFolder(next); setSelected(null); setSearch(''); };
  const breadcrumbs = folder ? folder.split('/') : [];
  const children = archiveChildren(state.data.entries, folder).filter(entry =>
    entry.path.split('/').pop()?.toLocaleLowerCase().includes(search.toLocaleLowerCase()));
  const memberUrl = selected ? archiveUrl(file, selected.path) : '';
  const member: ResourceItem | null = selected ? {
    id: url + ':' + selected.path,
    type: 'document',
    name: selected.path.split('/').pop() || selected.path,
    size: selected.size,
    download_url: memberUrl,
    origin: artifactOrigin(file),
    created_at: '',
    // Treat active text as text. Never render archive HTML/SVG in the app origin.
    mime_type: /\.(html?|svg)$/i.test(selected.path) ? 'text/plain' : undefined,
  } : null;

  return <div className="jx-archive">
    <div className="jx-archive-nav">
      {selected && <button type="button" aria-label={t('返回文件列表')} onClick={() => setSelected(null)}><ArrowLeftOutlined />{t('返回文件列表')}</button>}
      <nav aria-label={t('压缩包目录')}>
        <button type="button" onClick={() => navigate('')}>{t('压缩包根目录')}</button>
        {breadcrumbs.map((part, index) => <span key={index}> / <button type="button"
          onClick={() => navigate(breadcrumbs.slice(0, index + 1).join('/'))}>{part}</button></span>)}
      </nav>
      {!selected && folder && <button type="button" aria-label={t('上级目录')} onClick={() => navigate(breadcrumbs.slice(0, -1).join('/'))}>
        <ArrowLeftOutlined />{t('上级目录')}</button>}
    </div>
    {member ? <div className="jx-archive-preview" key={member.id}>{renderMember(member)}</div> : <>
      <div className="jx-archive-toolbar">
        <input type="search" aria-label={t('搜索当前目录')} placeholder={t('搜索当前目录')}
          value={search} onChange={event => setSearch(event.target.value)} />
        <span>{t('{count} 个条目', { count: children.length })}</span>
      </div>
      {state.data.blocked_count > 0 && <div className="jx-archive-notice">
        {t('已隐藏 {count} 个不安全路径', { count: state.data.blocked_count })}
      </div>}
      {children.length === 0 ? <div className="jx-archive-status">{t('此目录没有文件')}</div> :
        <ul className="jx-archive-list" aria-label={t('压缩包文件列表')}>
          {children.map(entry => {
            const name = entry.path.split('/').pop() || entry.path;
            return <li key={entry.path}>
              <button type="button" className="jx-archive-entry" aria-label={name}
                disabled={!entry.is_dir && !entry.readable}
                title={entry.reason ? archiveError(entry.reason) : name}
                onClick={() => entry.is_dir ? navigate(entry.path) : setSelected(entry)}>
                {entry.is_dir ? <FolderOutlined /> : entry.reason === 'archive_encrypted' ? <LockOutlined /> :
                  <img src={getFileIconSrc(name)} width={20} height={20} alt="" />}
                <span className="jx-archive-name">{name}
                  {entry.reason && <small>{archiveError(entry.reason)}</small>}
                </span>
                <span className="jx-archive-size">{entry.is_dir ? t('文件夹') : formatPreviewBytes(entry.size)}</span>
              </button>
              {!entry.is_dir && entry.readable && <a className="jx-archive-download"
                href={archiveUrl(file, entry.path)} download={name} aria-label={t('下载 {name}', { name })}>
                <DownloadOutlined />
              </a>}
            </li>;
          })}
        </ul>}
    </>}
  </div>;
}
