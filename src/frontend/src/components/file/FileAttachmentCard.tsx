import { LoadingOutlined, DownloadOutlined, CloseOutlined } from '@ant-design/icons';
import { getFileIconSrc } from '../../utils/fileIcon';
import { t } from '../../i18n';
import { useCanvasStore, type CanvasArtifact } from '../../stores/canvasStore';

const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg']);

export default function FileAttachmentCard({
  name,
  downloadHref,
  onClose,
  loading,
  previewUrl,
  artifact,
}: {
  name: string;
  downloadHref?: string;
  onClose?: () => void;
  loading?: boolean;
  previewUrl?: string;
  artifact?: CanvasArtifact;
}) {
  const openCanvas = useCanvasStore((s) => s.openCanvas);
  const canPreview = !!artifact && !loading;
  const ext = (name.split('.').pop() ?? '').toLowerCase();
  const isImage = IMAGE_EXTS.has(ext);

  let label = t('文档');
  if (ext === 'pdf')                           { label = t('PDF 文档'); }
  else if (ext === 'docx' || ext === 'doc')    { label = t('Word 文档'); }
  else if (ext === 'xlsx' || ext === 'xls')    { label = t('Excel 表格'); }
  else if (ext === 'pptx' || ext === 'ppt')    { label = t('PPT 幻灯片'); }
  else if (ext === 'wps')                      { label = t('WPS 文档'); }
  else if (ext === 'csv')                      { label = t('CSV 表格'); }
  else if (ext === 'txt')                      { label = t('文本文件'); }
  else if (isImage)                            { label = t('图片'); }

  const inner = (
    <>
      <div className="jx-fileCard-icon">
        {isImage && previewUrl ? (
          <img src={previewUrl} alt={name} className="jx-fileCard-imgThumb" />
        ) : (
          <img src={getFileIconSrc(name)} width="24" height="24" alt="" aria-hidden="true" />
        )}
      </div>
      <div className="jx-fileCard-info">
        <div className="jx-fileCard-name" title={name}>{name}</div>
        <div className="jx-fileCard-type">
          {loading ? <><LoadingOutlined style={{ marginRight: 4 }} />{t('上传中…')}</> : label}
        </div>
      </div>
      {!loading && downloadHref && !canPreview && (
        <DownloadOutlined className="jx-fileCard-dlIcon" aria-hidden="true" />
      )}
      {onClose && (
        <button className="jx-fileCard-close" onClick={(e) => { e.preventDefault(); e.stopPropagation(); onClose(); }} aria-label={t('移除文件')} title={t('移除')}>
          <CloseOutlined style={{ fontSize: 9 }} />
        </button>
      )}
    </>
  );

  if (canPreview) {
    return (
      <div className="jx-fileCard jx-fileCard--preview">
        {inner}
        <button type="button" className="jx-fileCard-preview"
          aria-label={`${t('预览')} ${name}`} title={`${t('预览')} ${name}`}
          onClick={() => openCanvas(artifact)} />
        {downloadHref && (
          <a className="jx-fileCard-download" href={downloadHref} download={name}
            aria-label={t('下载 {name}', { name })} title={t('下载 {name}', { name })}>
            <DownloadOutlined aria-hidden="true" />
          </a>
        )}
      </div>
    );
  }

  if (downloadHref && !loading) {
    return (
      <a
        className="jx-fileCard jx-fileCard--link"
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

  return <div className="jx-fileCard">{inner}</div>;
}
