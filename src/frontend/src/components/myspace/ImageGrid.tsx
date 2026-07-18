import { useCallback, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { CopyOutlined, DeleteOutlined, DownloadOutlined, EditOutlined, ExportOutlined, FolderAddOutlined, FolderOpenOutlined, FolderOutlined } from '@ant-design/icons';
import { Dropdown, Tooltip } from 'antd';
import type { PersonalFolderNode, ResourceItem } from '../../types';
import { useUIStore } from '../../stores';
import { buildFileUrl } from '../../utils/constants';
import { confirmDelete } from '../../utils/confirmDelete';
import { EASE, LAYOUT_ANIM_MAX_ITEMS } from '../../utils/motionTokens';
import { t } from '../../i18n';

interface ImageGridProps {
  items: ResourceItem[];
  onDownload: (item: ResourceItem) => void;
  onNavigate?: (item: ResourceItem) => void;
  onDelete?: (item: ResourceItem) => void;
  onMoveToTeam?: (item: ResourceItem) => void;
  onMoveToPersonalFolder?: (item: ResourceItem) => void;
  onCopyToPersonalFolder?: (item: ResourceItem) => void;
  // ── Personal folder (optional) ──
  folders?: PersonalFolderNode[];
  onEnterFolder?: (folderId: string) => void;
  onRenameFolder?: (folderId: string, currentName: string) => void;
  onDeleteFolder?: (folderId: string, name: string) => void;
}

function ImageThumbnail({ item }: { item: ResourceItem }) {
  // onLoad fade-in: stay transparent until decoding completes, to avoid flicker from progressive rendering
  const [loaded, setLoaded] = useState(false);

  if (!item.file_id) {
    return (
      <div className="jx-mySpace-imgCell">
        <div className="jx-mySpace-imgPlaceholder">{t('无文件')}</div>
      </div>
    );
  }

  return (
    <div className="jx-mySpace-imgCell">
      <img
        src={buildFileUrl(item.file_id)}
        alt={item.name}
        className={`jx-mySpace-imgThumb${loaded ? ' loaded' : ''}`}
        loading="lazy"
        onLoad={() => setLoaded(true)}
        onError={(e) => {
          const el = e.currentTarget;
          el.style.display = 'none';
          el.parentElement?.classList.add('jx-mySpace-imgCell--error');
        }}
      />
    </div>
  );
}

export function ImageGrid({
  items,
  onDownload,
  onNavigate,
  onDelete,
  onMoveToTeam,
  onMoveToPersonalFolder,
  onCopyToPersonalFolder,
  folders,
  onEnterFolder,
  onRenameFolder,
  onDeleteFolder,
}: ImageGridProps) {
  const { setPreviewImage } = useUIStore();

  const handlePreview = useCallback((item: ResourceItem) => {
    if (!item.file_id) return;
    setPreviewImage({ url: buildFileUrl(item.file_id), name: item.name });
  }, [setPreviewImage]);

  const handleDelete = useCallback((item: ResourceItem) => {
    confirmDelete(item.name, () => onDelete?.(item), '图片');
  }, [onDelete]);

  const folderRows = folders ?? [];
  if (items.length === 0 && folderRows.length === 0) return null;

  return (
    <div className="jx-mySpace-imgGrid">
      {folderRows.map((f) => {
        const menuItems: any[] = [];
        if (onEnterFolder) menuItems.push({ key: 'enter', label: t('打开'), onClick: () => onEnterFolder(f.folder_id) });
        if (onRenameFolder) menuItems.push({ key: 'rename', icon: <EditOutlined />, label: t('重命名'), onClick: () => onRenameFolder(f.folder_id, f.name) });
        if (onDeleteFolder) {
          menuItems.push({ type: 'divider' });
          menuItems.push({ key: 'delete', label: <span style={{ color: '#ff4d4f' }}>{t('删除文件夹')}</span>, onClick: () => onDeleteFolder(f.folder_id, f.name) });
        }
        return (
          <div
            key={`folder-${f.folder_id}`}
            className="jx-mySpace-imgItem jx-mySpace-imgItem--folder"
            onDoubleClick={() => onEnterFolder?.(f.folder_id)}
            onClick={() => onEnterFolder?.(f.folder_id)}
            style={{ cursor: onEnterFolder ? 'pointer' : 'default' }}
          >
            <div className="jx-mySpace-imgCell" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <FolderOutlined style={{ fontSize: 56, color: '#FFB72E' }} />
            </div>
            <div style={{ padding: '4px 8px', textAlign: 'center', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {f.name}
            </div>
            {menuItems.length > 0 && (
              <div className="jx-mySpace-imgOverlay" onClick={(e) => e.stopPropagation()}>
                <Dropdown trigger={['click']} menu={{ items: menuItems }}>
                  <button className="jx-mySpace-actionBtn">
                    <FolderOpenOutlined />
                  </button>
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
          onClick={() => handlePreview(item)}
        >
          <ImageThumbnail item={item} />
          <div className="jx-mySpace-imgOverlay">
            <Tooltip title={t('下载')}>
              <button className="jx-mySpace-actionBtn" onClick={(e) => { e.stopPropagation(); onDownload(item); }}>
                <DownloadOutlined />
              </button>
            </Tooltip>
            {onNavigate && item.source_chat_id && (
              <Tooltip title={t('跳转到对话')}>
                <button className="jx-mySpace-actionBtn" onClick={(e) => { e.stopPropagation(); onNavigate(item); }}>
                  <ExportOutlined />
                </button>
              </Tooltip>
            )}
            {onMoveToPersonalFolder && (
              <Tooltip title={t('移至文件夹')}>
                <button className="jx-mySpace-actionBtn" onClick={(e) => { e.stopPropagation(); onMoveToPersonalFolder(item); }}>
                  <FolderOpenOutlined />
                </button>
              </Tooltip>
            )}
            {onCopyToPersonalFolder && (
              <Tooltip title={t('复制到文件夹')}>
                <button className="jx-mySpace-actionBtn" onClick={(e) => { e.stopPropagation(); onCopyToPersonalFolder(item); }}>
                  <CopyOutlined />
                </button>
              </Tooltip>
            )}
            {onMoveToTeam && (
              <Tooltip title={t('移至团队')}>
                <button className="jx-mySpace-actionBtn" onClick={(e) => { e.stopPropagation(); onMoveToTeam(item); }}>
                  <FolderAddOutlined />
                </button>
              </Tooltip>
            )}
            {onDelete && (
              <Tooltip title={t('删除')}>
                <button className="jx-mySpace-actionBtn jx-mySpace-actionBtn--danger" onClick={(e) => { e.stopPropagation(); handleDelete(item); }}>
                  <DeleteOutlined />
                </button>
              </Tooltip>
            )}
          </div>
        </motion.div>
      ))}
      </AnimatePresence>
    </div>
  );
}
