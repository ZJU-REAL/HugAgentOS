import { useState, useCallback, useEffect } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Checkbox, Dropdown, Modal } from 'antd';
import { MoreOutlined } from '@ant-design/icons';
import type { PersonalFolderNode, ResourceItem } from '../../types';
import { getFolderIconSrc } from '../../utils/fileIcon';
import { LAYOUT_ANIM_MAX_ITEMS } from '../../utils/motionTokens';
import { LIST_ITEM_EXIT } from '../../utils/motionVariants';
import { BulkActionBar } from './BulkActionBar';
import { ResourceCard } from './ResourceCard';
import { t } from '../../i18n';

interface DocumentListProps {
  items: ResourceItem[];
  onDownload: (item: ResourceItem) => void;
  onNavigate?: (item: ResourceItem) => void;
  onDelete?: (item: ResourceItem) => void;
  onPreview?: (item: ResourceItem) => void;
  onAddToKb?: (items: ResourceItem[]) => void;
  scopeKind?: 'personal' | 'team';
  /** Whether write operations (upload/delete/move) are allowed under team scope. */
  canEditCurrent?: boolean;
  /** Whether the user is admin under team scope; controls the ability to delete/move all files rather than only self-uploaded ones. */
  canAdminCurrent?: boolean;
  /** Bulk move to another team folder (source: personal). */
  onBulkMoveToTeam?: (items: ResourceItem[]) => void;
  /** Move a single file to a team folder (source: personal). */
  onMoveToTeam?: (item: ResourceItem) => void;
  /** Copy a single file to a team folder (source: personal, non-destructive). */
  onCopyToTeam?: (item: ResourceItem) => void;
  /** Bulk copy to a team folder (source: personal). */
  onBulkCopyToTeam?: (items: ResourceItem[]) => void;
  // ── Personal folders (optional; only used when scopeKind=personal) ──
  /** Direct child folders at the current level, rendered before the file list. */
  folders?: PersonalFolderNode[];
  /** Double-click folder → enter. */
  onEnterFolder?: (folderId: string) => void;
  onRenameFolder?: (folderId: string, currentName: string) => void;
  onDeleteFolder?: (folderId: string, name: string) => void;
  /** Copy an entire personal folder to a team (recursive). */
  onCopyFolderToTeam?: (folderId: string, name: string) => void;
  onMoveToPersonalFolder?: (item: ResourceItem) => void;
  onBulkMoveToPersonalFolder?: (items: ResourceItem[]) => void;
  /** Copy to a personal folder (keep original, create a new copy). */
  onCopyToPersonalFolder?: (item: ResourceItem) => void;
  onBulkCopyToPersonalFolder?: (items: ResourceItem[]) => void;
}

export function DocumentList({
  items, onDownload, onNavigate, onDelete, onPreview, onAddToKb,
  scopeKind = 'personal',
  canEditCurrent = false,
  canAdminCurrent = false,
  onBulkMoveToTeam,
  onMoveToTeam,
  onCopyToTeam,
  onBulkCopyToTeam,
  folders,
  onEnterFolder,
  onRenameFolder,
  onDeleteFolder,
  onCopyFolderToTeam,
  onMoveToPersonalFolder,
  onBulkMoveToPersonalFolder,
  onCopyToPersonalFolder,
  onBulkCopyToPersonalFolder,
}: DocumentListProps) {
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    setSelectedIds((prev) => {
      const next = new Set<string>();
      items.forEach((item) => {
        if (prev.has(item.id)) next.add(item.id);
      });
      return next;
    });
  }, [items]);

  const allSelected = items.length > 0 && items.every((i) => selectedIds.has(i.id));
  const someSelected = items.some((i) => selectedIds.has(i.id));
  const anySelected = someSelected;
  const selectedCount = selectedIds.size;

  const handleCheckItem = useCallback((id: string, checked: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  const handleSelectAll = useCallback((checked: boolean) => {
    if (checked) {
      setSelectedIds(new Set(items.map((i) => i.id)));
    } else {
      setSelectedIds(new Set());
    }
  }, [items]);

  const selectedItems = items.filter((i) => selectedIds.has(i.id));

  const handleBulkDownload = () => {
    selectedItems.forEach((item) => {
      if (item.file_id) onDownload(item);
    });
  };

  const handleBulkAddToKb = () => {
    const validItems = selectedItems.filter((item) => item.file_id);
    if (validItems.length > 0) {
      onAddToKb?.(validItems);
    }
  };

  const handleBulkDelete = () => {
    if (!onDelete) return;
    Modal.confirm({
      title: t('确认删除 {n} 个文件', { n: selectedCount }),
      content: t('确定要删除选中的 {n} 个文件吗？此操作不可撤销。', { n: selectedCount }),
      okText: t('删除'),
      cancelText: t('取消'),
      okButtonProps: { danger: true },
      onOk: () => {
        selectedItems.forEach((item) => onDelete(item));
        setSelectedIds(new Set());
      },
    });
  };

  const folderRows = folders ?? [];
  if (items.length === 0 && folderRows.length === 0) return null;

  return (
    <>
      <div className={`jx-mySpace-docTable${anySelected ? ' jx-mySpace-docTable--hasSelection' : ''}`}>
        {/* Table header */}
        <div className="jx-mySpace-docTable-header">
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--check">
            <Checkbox
              checked={allSelected}
              indeterminate={someSelected && !allSelected}
              onChange={(e) => handleSelectAll(e.target.checked)}
            />
          </div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--name">{t('名称')}</div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--size">{t('大小')}</div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--source">{t('来源')}</div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--time">{t('最近更新')}</div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--actions" />
        </div>

        {/* Folder rows (same table-row style as file rows, placed before files) */}
        {folderRows.map((f) => {
          const folderMenu: any[] = [];
          if (onEnterFolder) folderMenu.push({ key: 'enter', label: t('打开'), onClick: () => onEnterFolder(f.folder_id) });
          if (onRenameFolder) folderMenu.push({ key: 'rename', label: t('重命名'), onClick: () => onRenameFolder(f.folder_id, f.name) });
          if (onCopyFolderToTeam) folderMenu.push({ key: 'copyToTeam', label: t('复制到团队文件夹'), onClick: () => onCopyFolderToTeam(f.folder_id, f.name) });
          if (onDeleteFolder) {
            if (folderMenu.length > 0) folderMenu.push({ type: 'divider' });
            folderMenu.push({
              key: 'delete',
              label: <span style={{ color: 'var(--color-error, #ff4d4f)' }}>{t('删除文件夹')}</span>,
              onClick: () => onDeleteFolder(f.folder_id, f.name),
            });
          }
          return (
            <div
              key={`folder-${f.folder_id}`}
              className="jx-mySpace-docRow jx-mySpace-docRow--folder"
              onClick={() => onEnterFolder?.(f.folder_id)}
              onDoubleClick={() => onEnterFolder?.(f.folder_id)}
              style={{ cursor: onEnterFolder ? 'pointer' : 'default' }}
              title={t('双击打开 {name}', { name: f.name })}
            >
              <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--check" />
              <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--name">
                <img
                  className="jx-mySpace-docRow-icon"
                  src={getFolderIconSrc()}
                  alt=""
                  aria-hidden="true"
                />
                <div className="jx-mySpace-docRow-nameWrap">
                  <span className="jx-mySpace-docRow-name" title={f.name}>{f.name}</span>
                </div>
              </div>
              <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--size">
                <span className="jx-mySpace-docRow-meta">—</span>
              </div>
              <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--source">
                <span className="jx-mySpace-docRow-meta">{t('文件夹')}</span>
              </div>
              <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--time">
                <span className="jx-mySpace-docRow-time">
                  {f.created_at ? new Date(f.created_at).toLocaleDateString() : ''}
                </span>
              </div>
              <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--actions">
                {folderMenu.length > 0 && (
                  <Dropdown
                    menu={{
                      items: folderMenu,
                      onClick: ({ domEvent }) => domEvent.stopPropagation(),
                    }}
                    trigger={['click']}
                    placement="bottomRight"
                  >
                    <button
                      type="button"
                      className="jx-mySpace-moreBtn"
                      onClick={(e) => e.stopPropagation()}
                      title={t('更多操作')}
                    >
                      <MoreOutlined />
                    </button>
                  </Dropdown>
                )}
              </div>
            </div>
          );
        })}

        {/* File rows: delete exit animation + popLayout backfill (loadMore appends do not replay — initial=false and key bound to business id) */}
        <AnimatePresence mode="popLayout" initial={false}>
          {items.map((item) => (
            <motion.div
              key={item.id}
              layout={items.length <= LAYOUT_ANIM_MAX_ITEMS ? 'position' : false}
              exit={LIST_ITEM_EXIT}
            >
              <ResourceCard
                item={item}
                checked={selectedIds.has(item.id)}
                anySelected={anySelected}
                onCheck={(checked) => handleCheckItem(item.id, checked)}
                onDownload={onDownload}
                onNavigate={onNavigate}
                onDelete={onDelete}
                onPreview={onPreview}
                onAddToKb={onAddToKb ? (resource) => onAddToKb([resource]) : undefined}
                onMoveToTeam={onMoveToTeam}
                onCopyToTeam={onCopyToTeam}
                onMoveToPersonalFolder={onMoveToPersonalFolder}
                onCopyToPersonalFolder={onCopyToPersonalFolder}
              />
            </motion.div>
          ))}
        </AnimatePresence>
      </div>

      {/* Bulk-action floating bar (shared component: Portal + AnimatePresence + count spring) */}
      <BulkActionBar open={anySelected} count={selectedCount}>
          {onAddToKb && (
            <button type="button" className="jx-mySpace-bulkBar-btn" onClick={handleBulkAddToKb}>
              <span>{t('加入知识库')}</span>
            </button>
          )}
          {onBulkMoveToPersonalFolder && scopeKind === 'personal' && (
            <button
              type="button"
              className="jx-mySpace-bulkBar-btn"
              onClick={() => onBulkMoveToPersonalFolder(selectedItems)}
            >
              <span>{t('移动到文件夹')}</span>
            </button>
          )}
          {onBulkCopyToPersonalFolder && scopeKind === 'personal' && (
            <button
              type="button"
              className="jx-mySpace-bulkBar-btn"
              onClick={() => onBulkCopyToPersonalFolder(selectedItems)}
            >
              <span>{t('复制到文件夹')}</span>
            </button>
          )}
          {onBulkMoveToTeam && scopeKind === 'personal' && (
            <button
              type="button"
              className="jx-mySpace-bulkBar-btn"
              onClick={() => onBulkMoveToTeam(selectedItems)}
            >
              <span>{t('移动到团队文件夹')}</span>
            </button>
          )}
          {onBulkCopyToTeam && scopeKind === 'personal' && (
            <button
              type="button"
              className="jx-mySpace-bulkBar-btn"
              onClick={() => onBulkCopyToTeam(selectedItems.filter((i) => i.file_id))}
            >
              <span>{t('复制到团队文件夹')}</span>
            </button>
          )}
          <button type="button" className="jx-mySpace-bulkBar-btn" onClick={handleBulkDownload}>
            <span>{t('下载')}</span>
          </button>
          {(scopeKind === 'personal' || canEditCurrent || canAdminCurrent) && onDelete && (
            <button type="button" className="jx-mySpace-bulkBar-btn jx-mySpace-bulkBar-btn--danger" onClick={handleBulkDelete}>
              <span>{t('删除')}</span>
            </button>
          )}
          <div className="jx-mySpace-bulkBar-divider" />
          <button
            type="button"
            className="jx-mySpace-bulkBar-btn jx-mySpace-bulkBar-btn--cancel"
            onClick={() => setSelectedIds(new Set())}
          >
            {t('取消')}
          </button>
      </BulkActionBar>
    </>
  );
}
