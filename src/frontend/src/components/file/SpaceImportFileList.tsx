import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Checkbox, Empty } from "antd";
import { PictureOutlined } from "@ant-design/icons";
import { authFetch, getApiUrl, LOCAL_TARGET_HEADER } from "../../api";
import { t } from "../../i18n";
import type { ResourceItem } from "../../types";
import { getFileIconSrc } from "../../utils/fileIcon";
import { formatDateKey } from "../../utils/date";

function formatSize(bytes?: number) {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export const IMAGE_NAME = /\.(png|jpe?g|gif|webp|bmp|svg|avif|ico)$/i;

/** 空间文件走鉴权接口，<img> 直链带不上 token，先取 blob 再显示。 */
function Thumbnail({ item }: { item: ResourceItem }) {
  const [url, setUrl] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = '';
    void authFetch(getApiUrl() + item.download_url, {
      signal: controller.signal, headers: { [LOCAL_TARGET_HEADER]: 'cloud' },
    }).then(async response => {
      if (!response.ok) throw new Error(String(response.status));
      const blob = await response.blob();
      if (!controller.signal.aborted) { objectUrl = URL.createObjectURL(blob); setUrl(objectUrl); }
    }).catch(() => { if (!controller.signal.aborted) setUrl(''); });
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [item.download_url]);
  return url
    ? <img src={url} alt={item.name} className="jx-spaceImportItem-thumb" />
    : <PictureOutlined style={{ fontSize: 22, color: 'var(--color-primary)' }} />;
}

interface FileListProps {
  items: ResourceItem[];
  selected: Set<string>;
  previewId: string | null;
  onToggle: (id: string) => void;
  onPreview: (item: ResourceItem) => void;
  /** 滚动到底自动翻页；与列表底部的「加载更多」按钮是同一个动作。 */
  onScrollEnd?: () => void;
  footer?: ReactNode;
}

export function FileListSkeleton({ count = 5 }: { count?: number }) {
  return (
    <div className="jx-spaceImportList" aria-hidden="true" aria-busy="true">
      {Array.from({ length: count }).map((_, idx) => (
        <div key={idx} className="jx-spaceImportItem jx-spaceImportItem--skeleton">
          <div className="jx-skeletonBlock jx-spaceImportItem-skCheckbox" />
          <div className="jx-spaceImportItem-icon">
            <div className="jx-skeletonBlock jx-spaceImportItem-skIcon" />
          </div>
          <div className="jx-spaceImportItem-info">
            <div className="jx-skeletonBlock jx-spaceImportItem-skName" />
            <div className="jx-spaceImportItem-meta">
              <div className="jx-skeletonBlock jx-spaceImportItem-skMeta" />
              <div className="jx-skeletonBlock jx-spaceImportItem-skMeta" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export function FileList({ items, selected, previewId, onToggle, onPreview, onScrollEnd, footer }: FileListProps) {
  const handleScroll = (e: React.UIEvent<HTMLDivElement>) => {
    if (!onScrollEnd) return;
    const el = e.currentTarget;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 100) onScrollEnd();
  };
  if (items.length === 0) {
    return <Empty description={t('暂无文件')} image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ margin: '32px 0' }} />;
  }
  return (
    <div className="jx-spaceImportList" onScroll={handleScroll}>
      {items.map((item) => {
        const isPreview = previewId === item.id;
        return (
          <div
            key={item.id}
            className={`jx-spaceImportItem${selected.has(item.id) ? ' selected' : ''}${isPreview ? ' previewing' : ''}`}
            onClick={() => onPreview(item)}
          >
            <Checkbox
              checked={selected.has(item.id)}
              onChange={() => onToggle(item.id)}
              onClick={(e) => e.stopPropagation()}
            />
            <div className="jx-spaceImportItem-icon">
              {item.type === 'image' ? (
                <Thumbnail item={item} />
              ) : (
                <img src={getFileIconSrc(item.name)} width={24} height={24} alt="" />
              )}
            </div>
            <div className="jx-spaceImportItem-info">
              <div className="jx-spaceImportItem-name" title={item.name}>{item.name}</div>
              <div className="jx-spaceImportItem-meta">
                {item.size ? <span>{formatSize(item.size)}</span> : null}
                <span>{item.created_at ? formatDateKey(item.created_at) : null}</span>
              </div>
            </div>
          </div>
        );
      })}
      {footer}
    </div>
  );
}
