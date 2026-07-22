import { useCallback, useState } from 'react';
import {
  CopyOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  ExportOutlined,
  FolderOpenOutlined,
  FolderOutlined,
} from '@ant-design/icons';
import { Dropdown, Tooltip } from 'antd';
import { AnimatePresence, motion } from 'motion/react';

import { useUIStore } from '../../stores';
import { t } from '../../i18n';
import type { PersonalFolderNode, ResourceItem } from '../../types';
import { buildFileUrl } from '../../utils/constants';
import { confirmDelete } from '../../utils/confirmDelete';
import { EASE, LAYOUT_ANIM_MAX_ITEMS } from '../../utils/motionTokens';

interface ImageGridProps {
  items: ResourceItem[];
  onDownload: (item: ResourceItem) => void;
  onNavigate?: (item: ResourceItem) => void;
  onDelete?: (item: ResourceItem) => void;
  onMoveToPersonalFolder?: (item: ResourceItem) => void;
  onCopyToPersonalFolder?: (item: ResourceItem) => void;
  folders?: PersonalFolderNode[];
  onEnterFolder?: (folderId: string) => void;
  onRenameFolder?: (folderId: string, currentName: string) => void;
  onDeleteFolder?: (folderId: string, name: string) => void;
}

function ImageThumbnail({ item }: { item: ResourceItem }) {
  const [loaded, setLoaded] = useState(false);
  if (!item.file_id) return <div className="jx-mySpace-imgPlaceholder">{t('无文件')}</div>;
  return (
    <div className="jx-mySpace-imgCell">
      <img
        src={buildFileUrl(item.file_id)}
        alt={item.name}
        className={`jx-mySpace-imgThumb${loaded ? ' loaded' : ''}`}
        loading="lazy"
        onLoad={() => setLoaded(true)}
      />
    </div>
  );
}

export function ImageGrid({
  items,
  onDownload,
  onNavigate,
  onDelete,
  onMoveToPersonalFolder,
  onCopyToPersonalFolder,
  folders,
  onEnterFolder,
  onRenameFolder,
  onDeleteFolder,
}: ImageGridProps) {
  const setPreviewImage = useUIStore((state) => state.setPreviewImage);
  const preview = useCallback((item: ResourceItem) => {
    if (item.file_id) setPreviewImage({ url: buildFileUrl(item.file_id), name: item.name });
  }, [setPreviewImage]);

  if (items.length === 0 && !folders?.length) return null;
  return (
    <div className="jx-mySpace-imgGrid">
      {(folders ?? []).map((folder) => {
        const menuItems: any[] = [];
        if (onRenameFolder) menuItems.push({ key: 'rename', icon: <EditOutlined />, label: t('重命名'), onClick: () => onRenameFolder(folder.folder_id, folder.name) });
        if (onDeleteFolder) menuItems.push({ key: 'delete', label: t('删除文件夹'), onClick: () => onDeleteFolder(folder.folder_id, folder.name) });
        return (
          <div key={folder.folder_id} className="jx-mySpace-imgItem jx-mySpace-imgItem--folder" onDoubleClick={() => onEnterFolder?.(folder.folder_id)}>
            <div className="jx-mySpace-imgCell" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <FolderOutlined style={{ fontSize: 56, color: '#FFB72E' }} />
            </div>
            <div style={{ padding: '4px 8px', textAlign: 'center' }}>{folder.name}</div>
            {menuItems.length > 0 && (
              <div className="jx-mySpace-imgOverlay">
                <Dropdown trigger={['click']} menu={{ items: menuItems }}>
                  <button type="button" className="jx-mySpace-actionBtn"><FolderOpenOutlined /></button>
                </Dropdown>
              </div>
            )}
          </div>
        );
      })}
      <AnimatePresence mode="popLayout" initial={false}>
        {items.map((item) => (
          <motion.div
            key={item.id}
            layout={items.length <= LAYOUT_ANIM_MAX_ITEMS ? 'position' : false}
            exit={{ opacity: 0, scale: 0.9, transition: { duration: 0.18, ease: EASE.exit } }}
            className="jx-mySpace-imgItem"
            onClick={() => preview(item)}
          >
            <ImageThumbnail item={item} />
            <div className="jx-mySpace-imgOverlay">
              <Tooltip title={t('下载')}>
                <button type="button" className="jx-mySpace-actionBtn" onClick={(event) => { event.stopPropagation(); onDownload(item); }}><DownloadOutlined /></button>
              </Tooltip>
              {onNavigate && item.source_chat_id && (
                <Tooltip title={t('跳转到对话')}>
                  <button type="button" className="jx-mySpace-actionBtn" onClick={(event) => { event.stopPropagation(); onNavigate(item); }}><ExportOutlined /></button>
                </Tooltip>
              )}
              {onMoveToPersonalFolder && (
                <Tooltip title={t('移至文件夹')}>
                  <button type="button" className="jx-mySpace-actionBtn" onClick={(event) => { event.stopPropagation(); onMoveToPersonalFolder(item); }}><FolderOpenOutlined /></button>
                </Tooltip>
              )}
              {onCopyToPersonalFolder && (
                <Tooltip title={t('复制到文件夹')}>
                  <button type="button" className="jx-mySpace-actionBtn" onClick={(event) => { event.stopPropagation(); onCopyToPersonalFolder(item); }}><CopyOutlined /></button>
                </Tooltip>
              )}
              {onDelete && (
                <Tooltip title={t('删除')}>
                  <button type="button" className="jx-mySpace-actionBtn jx-mySpace-actionBtn--danger" onClick={(event) => {
                    event.stopPropagation();
                    confirmDelete(item.name, () => onDelete(item), '图片');
                  }}><DeleteOutlined /></button>
                </Tooltip>
              )}
            </div>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
