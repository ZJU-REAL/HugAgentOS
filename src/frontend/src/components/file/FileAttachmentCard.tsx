import { usePublicMediaUrl } from '../../hooks/usePublicMediaUrl';
import { LoadingOutlined, DownloadOutlined, CloseOutlined } from '@ant-design/icons';
import '../../styles/chat/attachment-polish.css';
import { getFileIconSrc } from '../../utils/fileIcon';
import { t } from '../../i18n';
import { artifactUrl } from '../../utils/artifactAccess';
import { ArtifactFileAction } from './ArtifactFileAction';
import { useCanvasStore, type CanvasArtifact } from '../../stores/canvasStore';

const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg']);

export default function FileAttachmentCard({
  name,
  downloadHref,
  onClose,
  loading,
  previewUrl,
  artifact,
  compact = false,
  error,
  onRetry,
}: {
  name: string;
  downloadHref?: string;
  onClose?: () => void;
  loading?: boolean;
  previewUrl?: string;
  artifact?: CanvasArtifact;
  compact?: boolean;
  error?: string;
  onRetry?: () => void;
}) {
  const openCanvas = useCanvasStore((s) => s.openCanvas);
  const canPreview = !!artifact && !loading && !error;
  if (artifact) {
    downloadHref = artifactUrl(artifact);
    if (previewUrl && !previewUrl.startsWith('blob:')) previewUrl = artifactUrl(artifact, { params: { inline: 'true' } });
  }
  const media = usePublicMediaUrl(previewUrl || '');
  const ext = (name.split('.').pop() ?? '').toLowerCase();
  const isImage = IMAGE_EXTS.has(ext);
  const className = `jx-fileCard${compact ? ' jx-fileCard--compact' : ''}${loading ? ' jx-fileCard--uploading' : ''}${error ? ' jx-fileCard--error' : ''}`;
  const dot = name.lastIndexOf('.');
  const basename = dot > 0 ? name.slice(0, dot) : name;
  const suffix = dot > 0 ? name.slice(dot) : '';

  const inner = (
    <>
      <div className="jx-fileCard-icon">
        {isImage && media.url ? (
          <img src={media.url} alt={name} className="jx-fileCard-imgThumb" />
        ) : (
          <img src={getFileIconSrc(name)} width="24" height="24" alt="" aria-hidden="true" />
        )}
      </div>
      <div className="jx-fileCard-info">
        <div className="jx-fileCard-name" title={name}>
          <span className="jx-fileCard-basename">{basename}</span><span className="jx-fileCard-extension">{suffix}</span>
        </div>
        {(loading || error) && (
          <div className="jx-fileCard-type">
            {loading ? <><LoadingOutlined style={{ marginRight: 4 }} />{t('上传中…')}</> : t('上传失败')}
          </div>
        )}
      </div>
      {!loading && !error && downloadHref && !canPreview && (
        <DownloadOutlined className="jx-fileCard-dlIcon" aria-hidden="true" />
      )}
      {onClose && (
        <button type="button" className="jx-fileCard-close" onClick={(e) => { e.preventDefault(); e.stopPropagation(); onClose(); }} aria-label={t('移除文件 {name}', { name })} title={t('移除')}>
          <CloseOutlined style={{ fontSize: 9 }} />
        </button>
      )}
      {error && <div className="jx-fileCard-error" role="status">
        <span title={error}>{error}</span>
        {onRetry && <button type="button" onClick={onRetry} aria-label={t('重试上传 {name}', { name })}>{t('重试')}</button>}
      </div>}
    </>
  );

  if (canPreview) {
    return (
      <div className={`${className} jx-fileCard--preview`}>
        {inner}
        <button type="button" className="jx-fileCard-preview"
          aria-label={`${t('预览')} ${name}`} title={`${t('预览')} ${name}`}
          onClick={() => openCanvas(artifact)} />
        {downloadHref && (
          <span className="jx-fileCard-download">
            <ArtifactFileAction file={artifact} iconOnly={compact} className="jx-fileCard-nativeAction" />
          </span>
        )}
      </div>
    );
  }

  if (downloadHref && !loading) {
    return (
      <a
        className={`${className} jx-fileCard--link`}
        href={downloadHref}
        download={name}
        title={t('下载 {name}', { name })}
        onClick={(e) => {
          e.preventDefault();
          const a = document.createElement('a');
          a.href = downloadHref;
          a.download = name;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
        }}
      >
        {inner}
      </a>
    );
  }

  return <div className={className} aria-busy={!!loading}>{inner}</div>;
}
