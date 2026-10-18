import { useEffect, useState } from 'react';
import { Dropdown } from 'antd';
import { DownloadOutlined, MoreOutlined } from '@ant-design/icons';
import { authFetch, getApiUrl, LOCAL_TARGET_HEADER } from '../../api';
import type { ResourceItem } from '../../types';
import { t } from '../../i18n';

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
  return <div className="jx-mySpace-imgCell">{url ? <img className="jx-mySpace-imgThumb loaded" src={url} alt={item.name} />
    : <span className="jx-mySpace-imgPlaceholder">{item.name}</span>}</div>;
}

export function SpaceImageGrid({ items, onPreview, onDownload, onRename, onDelete }: {
  items: ResourceItem[]; onPreview: (item: ResourceItem) => void; onDownload: (item: ResourceItem) => void; onRename?: (item: ResourceItem) => void; onDelete?: (item: ResourceItem) => void;
}) {
  return <div className="jx-mySpace-imgGrid">{items.map(item => <div key={item.id} className="jx-mySpace-imgItem"
    role="button" tabIndex={0} aria-label={item.name} onClick={() => onPreview(item)}
    onKeyDown={event => { if (event.key === 'Enter') onPreview(item); }}>
    <Thumbnail item={item} />
    <div className="jx-mySpace-imgOverlay"><button className="jx-mySpace-actionBtn" title={t('下载')}
      onClick={event => { event.stopPropagation(); onDownload(item); }}><DownloadOutlined /></button>
      {(onRename || onDelete) && <Dropdown trigger={['click']} menu={{ items: [
        ...(onRename ? [{ key: 'rename', label: t('重命名'), onClick: () => onRename(item) }] : []),
        ...(onDelete ? [{ key: 'delete', label: t('删除'), danger: true, onClick: () => onDelete(item) }] : []),
      ], onClick: ({ domEvent }) => domEvent.stopPropagation() }}>
        <button className="jx-mySpace-actionBtn" title={t('更多操作')} onClick={event => event.stopPropagation()}><MoreOutlined /></button>
      </Dropdown>}
    </div>
  </div>)}</div>;
}
