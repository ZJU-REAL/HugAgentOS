import { message } from 'antd';
import { CopyOutlined, DownloadOutlined, EyeOutlined } from '@ant-design/icons';
import { getFileIconSrc } from '../../utils/fileIcon';
import { useCanvasStore, useUIStore } from '../../stores';
import { t } from '../../i18n';

const effectiveApiUrl = (import.meta.env.VITE_API_BASE_URL as string || '').trim() || '/api';

function formatFileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function renderFileIcon(name: string) {
  return (
    <div className="jx-dlCard-fileBadge">
      <img src={getFileIconSrc(name)} width="28" height="28" alt="" aria-hidden="true" className="jx-dlCard-fileIcon" />
    </div>
  );
}

export interface ArtifactRef {
  file_id: string;
  name?: string;
  url?: string;
  mime_type?: string;
  size?: number;
}

/** Render a set of artifact (file) cards — images get inline previews with
 *  copy/download overlay buttons, other files get a download/preview card.
 *  Same primitives the regular chat bubble renders for assistant outputs;
 *  shared so the batch panel and chat list stay visually consistent. */
export function ArtifactCardList({ artifacts }: { artifacts: ArtifactRef[] }) {
  const setPreviewImage = useUIStore((s) => s.setPreviewImage);
  const openCanvas = useCanvasStore((s) => s.openCanvas);

  if (!artifacts || artifacts.length === 0) return null;

  return (
    <div className="jx-artifactCards">
      {artifacts.map((art) => {
        const isImage = typeof art.mime_type === 'string' && art.mime_type.startsWith('image/');
        const fileUrl = `${effectiveApiUrl}${art.url || ''}`;

        if (isImage) {
          return (
            <div
              key={art.file_id}
              className="jx-imgCard"
              role="button"
              tabIndex={0}
              aria-label={t('查看大图：{name}', { name: art.name || t('生成图片') })}
              onClick={() => setPreviewImage({ url: fileUrl, name: art.name || t('生成图片') })}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  setPreviewImage({ url: fileUrl, name: art.name || t('生成图片') });
                }
              }}
            >
              <img src={fileUrl} alt={art.name} className="jx-imgCard-img" loading="lazy" />
              <div className="jx-imgCard-overlay" />
              <div className="jx-imgCard-overlayBtns">
                <button
                  className="jx-imgCard-overlayBtn"
                  title={t('复制图片')}
                  onClick={async (e) => {
                    e.stopPropagation();
                    const getBlob = (): Promise<Blob> => new Promise((resolve, reject) => {
                      const i = new Image();
                      i.crossOrigin = 'anonymous';
                      i.onload = () => {
                        const c = document.createElement('canvas');
                        c.width = i.naturalWidth;
                        c.height = i.naturalHeight;
                        c.getContext('2d')!.drawImage(i, 0, 0);
                        c.toBlob((b) => (b ? resolve(b) : reject(new Error('toBlob failed'))), 'image/png');
                      };
                      i.onerror = () => reject(new Error('load failed'));
                      i.src = fileUrl;
                    });
                    if (navigator.clipboard && window.ClipboardItem) {
                      try {
                        const blob = await getBlob();
                        await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
                        message.success(t('图片已复制到剪贴板'));
                        return;
                      } catch { /* fall through */ }
                    }
                    try {
                      await new Promise<void>((resolve, reject) => {
                        const container = document.createElement('div');
                        container.setAttribute('contenteditable', 'true');
                        container.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0;pointer-events:none;';
                        const img = document.createElement('img');
                        img.onload = () => {
                          document.body.appendChild(container);
                          container.focus();
                          const sel = window.getSelection()!;
                          const range = document.createRange();
                          range.selectNodeContents(container);
                          sel.removeAllRanges();
                          sel.addRange(range);
                          const ok = document.execCommand('copy');
                          sel.removeAllRanges();
                          document.body.removeChild(container);
                          if (ok) resolve();
                          else reject(new Error('execCommand failed'));
                        };
                        img.onerror = () => reject(new Error('image load failed'));
                        img.src = fileUrl;
                        container.appendChild(img);
                      });
                      message.success(t('图片已复制到剪贴板'));
                    } catch {
                      message.error(t('复制失败，请右键图片选择"复制图片"'));
                    }
                  }}
                >
                  <CopyOutlined />
                </button>
                <a
                  href={fileUrl}
                  download={art.name}
                  className="jx-imgCard-overlayBtn"
                  title={t('下载图片')}
                  onClick={(e) => e.stopPropagation()}
                >
                  <DownloadOutlined />
                </a>
              </div>
            </div>
          );
        }

        return (
          <div key={art.file_id} className="jx-dlCard">
            <div className="jx-dlCard-left">
              {renderFileIcon(art.name || '')}
              <div className="jx-dlCard-meta">
                <span className="jx-dlCard-name">{art.name}</span>
                {art.size && <span className="jx-dlCard-size">{formatFileSize(art.size)}</span>}
              </div>
            </div>
            <div className="jx-dlCard-actions">
              <button
                className="jx-dlCard-previewBtn"
                onClick={() => openCanvas({
                  file_id: art.file_id,
                  name: art.name || t('文件'),
                  url: art.url || '',
                  mime_type: art.mime_type,
                  size: art.size,
                })}
              >
                <EyeOutlined /> {t('预览')}
              </button>
              <a href={fileUrl} download={art.name} className="jx-dlCard-btn">
                <DownloadOutlined /> {t('下载')}
              </a>
            </div>
          </div>
        );
      })}
    </div>
  );
}
