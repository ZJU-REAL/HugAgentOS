import { FileExclamationOutlined } from '@ant-design/icons';
import { t } from '../../../i18n';
import { getFileIconSrc } from '../../../utils/fileIcon';
import type { CanvasArtifact } from '../../../stores/canvasStore';
import { ArtifactFileAction } from '../../file/ArtifactFileAction';
import { formatPreviewBytes } from '../../../utils/filePreviewSafety';

export function UnknownRenderer({ name }: { name: string }) {
  return (
    <div className="jx-canvas-unknown">
      <img src={getFileIconSrc(name)} width="48" height="48" alt="" aria-hidden="true" />
      <p>{t('暂不支持预览此文件格式')}</p>
      <p className="jx-canvas-unknown-hint">{name}</p>
    </div>
  );
}

export function LargeFileRenderer({
  artifact,
  limitBytes,
  onDownload,
}: {
  artifact: CanvasArtifact;
  limitBytes: number;
  onDownload: () => void;
}) {
  return (
    <div className="jx-canvas-largeFile">
      <FileExclamationOutlined className="jx-canvas-largeFileIcon" />
      <p className="jx-canvas-largeFileTitle">{t('文件较大，已停止在线预览')}</p>
      <p className="jx-canvas-largeFileHint">
        {t('当前文件 {size}，超过此格式 {limit} 的安全预览上限。请使用下方文件操作在系统应用中查看。', {
          size: formatPreviewBytes(artifact.size || 0),
          limit: formatPreviewBytes(limitBytes),
        })}
      </p>
      <ArtifactFileAction file={artifact} className="jx-fileAction-button" onDownload={onDownload} />
    </div>
  );
}
