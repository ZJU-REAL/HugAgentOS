import { useCallback, useState, type ReactNode } from 'react';
import { ReloadOutlined } from '@ant-design/icons';
import { message } from 'antd';
import type { CanvasArtifact } from '../../stores/canvasStore';
import { artifactOrigin, isArchiveMember } from '../../utils/artifactAccess';
import { FileOpenControl, type FileActionPhase } from '../file/FileOpenControl';
import { ArtifactFileAction } from '../file/ArtifactFileAction';
import { FilePathLabel } from './FilePathLabel';
import { CanvasZoomControl } from './CanvasZoomControl';
import { useFileLocation } from './useFileLocation';
import { t } from '../../i18n';
import './CanvasFileHeader.css';

export function CanvasFileHeader({ artifact, revision, dirty, saving, onReload, onDownload, children }: {
  artifact: CanvasArtifact; revision: number; dirty: boolean; saving: boolean;
  onReload: () => void; onDownload?: () => void; children?: ReactNode;
}) {
  const location = useFileLocation(artifact, revision);
  const [phase, setPhase] = useState<FileActionPhase>();
  const announce = useCallback((value: FileActionPhase | undefined) => {
    setPhase(value);
    if (value?.kind === 'error') message.error(value.message);
  }, []);
  const local = artifactOrigin(artifact) === 'local' && !isArchiveMember(artifact);
  return <div className="jx-canvas-fileHeader" role="toolbar" aria-label={t('文件路径与操作')}>
    <FilePathLabel path={location.path || artifact.name} />
    {location.state === 'loading' && <span className="jx-fileHeader-status" role="status">{t('正在读取文件位置…')}</span>}
    {location.state === 'error' && <button type="button" className="jx-canvas-actionBtn"
      onClick={location.retry} title={t('重试获取文件位置')} aria-label={t('重试获取文件位置')}><ReloadOutlined /></button>}
    {location.state === 'unfiled' && <span className="jx-fileHeader-note">{t('尚未归档到我的空间')}</span>}
    <div className="jx-fileHeader-actions">
      <CanvasZoomControl />
      {children}
      <button type="button" className="jx-canvas-actionBtn" onClick={onReload} disabled={saving}
        aria-label={t('刷新预览')} title={t('刷新预览')}><ReloadOutlined /></button>
      {local ? <FileOpenControl file={artifact} onPath={location.nativePath} onPhase={announce} disabled={dirty || saving} />
        : <ArtifactFileAction file={artifact} iconOnly className="jx-canvas-actionBtn" onDownload={onDownload} />}
    </div>
    {phase && <span role="status" className="jx-fileHeader-status">{phase.message}</span>}
  </div>;
}
