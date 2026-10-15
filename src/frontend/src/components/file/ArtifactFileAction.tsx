import { authFetch, getApiUrl } from '../../api';
import { isPublicChat } from '../../publicChatTransport';
import { useState } from 'react';
import { Dropdown, message } from 'antd';
import { DownOutlined, DownloadOutlined, ExportOutlined, FolderOpenOutlined, LoadingOutlined } from '@ant-design/icons';
import { artifactOrigin, artifactUrl, isArchiveMember, openLocalArtifact, type ArtifactLocation } from '../../utils/artifactAccess';
import { t } from '../../i18n';
import './ArtifactFileAction.css';

/** Same file action in output cards and preview panes. Native paths never come from model output. */
export function ArtifactFileAction({ file, className, onDownload, disabled = false, iconOnly = false }: {
  file: ArtifactLocation & { name?: string };
  className?: string;
  onDownload?: () => void;
  disabled?: boolean;
  iconOnly?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const local = artifactOrigin(file) === 'local' && !isArchiveMember(file);
  const open = async (folder = false) => {
    if (busy || disabled) return;
    setBusy(true);
    try { await openLocalArtifact(file, folder); }
    catch (error) { message.error(error instanceof Error ? error.message : t('无法打开文件')); }
    finally { setBusy(false); }
  };
  if (!local) {
    return <a className={`jx-artifactAction ${className || ''}`} title={t('下载')} aria-label={t('下载 {name}', { name: file.name || t('文件') })} href={artifactUrl(file)} download={file.name}
      onClick={(event) => {
        event.stopPropagation();
        if (onDownload) { event.preventDefault(); onDownload(); }
        else if (isPublicChat() && new URL(artifactUrl(file), window.location.href).origin === new URL(getApiUrl(), window.location.href).origin) {
          event.preventDefault();
          if (busy) return;
          setBusy(true);
          void authFetch(artifactUrl(file)).then(async response => {
            if (!response.ok) throw new Error('HTTP ' + response.status);
            const blobUrl = URL.createObjectURL(await response.blob());
            const link = document.createElement('a');
            link.href = blobUrl; link.download = file.name || 'download'; link.click();
            window.setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
          }).catch(error => { message.error(String(error)); }).finally(() => setBusy(false));
        }
      }}><DownloadOutlined /> {!iconOnly && t('下载')}</a>;
  }
  return <span className={`jx-artifactAction jx-artifactOpen ${className || ''}`} onClick={(event) => event.stopPropagation()}>
    <button type="button" className="jx-artifactOpen-main" disabled={busy || disabled}
      onClick={() => void open()} aria-label={t('打开文件')} title={disabled ? t('请先保存文件') : t('打开文件')}>
      {busy ? <LoadingOutlined spin /> : <ExportOutlined />} {!iconOnly && t('打开')}
    </button>
    <Dropdown trigger={['click']} menu={{ items: [{
      key: 'folder', label: t('打开所在文件夹'), icon: <FolderOpenOutlined />,
      onClick: () => void open(true),
    }] }} disabled={busy || disabled}>
      <button type="button" className="jx-artifactOpen-menu" disabled={busy || disabled}
        aria-label={t('打开选项')} title={t('打开所在文件夹')}><DownOutlined /></button>
    </Dropdown>
  </span>;
}
