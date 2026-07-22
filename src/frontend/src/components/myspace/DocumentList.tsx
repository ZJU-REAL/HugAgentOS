import { useCallback, useEffect, useState } from 'react';
import { MoreOutlined } from '@ant-design/icons';
import { Checkbox, Dropdown, Modal } from 'antd';
import { AnimatePresence, motion } from 'motion/react';

import { t } from '../../i18n';
import type { PersonalFolderNode, ResourceItem } from '../../types';
import { getFolderIconSrc } from '../../utils/fileIcon';
import { LAYOUT_ANIM_MAX_ITEMS } from '../../utils/motionTokens';
import { LIST_ITEM_EXIT } from '../../utils/motionVariants';
import { BulkActionBar } from './BulkActionBar';
import { ResourceCard } from './ResourceCard';

interface DocumentListProps {
  items: ResourceItem[];
  onDownload: (item: ResourceItem) => void;
  onNavigate?: (item: ResourceItem) => void;
  onDelete?: (item: ResourceItem) => void;
  onPreview?: (item: ResourceItem) => void;
  onAddToKb?: (items: ResourceItem[]) => void;
  folders?: PersonalFolderNode[];
  onEnterFolder?: (folderId: string) => void;
  onRenameFolder?: (folderId: string, currentName: string) => void;
  onDeleteFolder?: (folderId: string, name: string) => void;
  onMoveToPersonalFolder?: (item: ResourceItem) => void;
  onBulkMoveToPersonalFolder?: (items: ResourceItem[]) => void;
  onCopyToPersonalFolder?: (item: ResourceItem) => void;
  onBulkCopyToPersonalFolder?: (items: ResourceItem[]) => void;
}

export function DocumentList({
  items,
  onDownload,
  onNavigate,
  onDelete,
  onPreview,
  onAddToKb,
  folders,
  onEnterFolder,
  onRenameFolder,
  onDeleteFolder,
  onMoveToPersonalFolder,
  onBulkMoveToPersonalFolder,
  onCopyToPersonalFolder,
  onBulkCopyToPersonalFolder,
}: DocumentListProps) {
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    setSelectedIds((previous) => new Set(items.filter((item) => previous.has(item.id)).map((item) => item.id)));
  }, [items]);

  const allSelected = items.length > 0 && items.every((item) => selectedIds.has(item.id));
  const someSelected = items.some((item) => selectedIds.has(item.id));
  const selectedItems = items.filter((item) => selectedIds.has(item.id));
  const handleSelect = useCallback((id: string, checked: boolean) => {
    setSelectedIds((previous) => {
      const next = new Set(previous);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);
  const folderRows = folders ?? [];
  if (items.length === 0 && folderRows.length === 0) return null;

  const handleBulkDelete = () => {
    if (!onDelete) return;
    Modal.confirm({
      title: t('确认删除 {n} 个文件', { n: selectedItems.length }),
      content: t('确定要删除选中的 {n} 个文件吗？此操作不可撤销。', { n: selectedItems.length }),
      okText: t('删除'),
      cancelText: t('取消'),
      okButtonProps: { danger: true },
      onOk: () => {
        selectedItems.forEach(onDelete);
        setSelectedIds(new Set());
      },
    });
  };

  return (
    <>
      <div className={`jx-mySpace-docTable${someSelected ? ' jx-mySpace-docTable--hasSelection' : ''}`}>
        <div className="jx-mySpace-docTable-header">
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--check">
            <Checkbox
              checked={allSelected}
              indeterminate={someSelected && !allSelected}
              onChange={(event) => setSelectedIds(event.target.checked ? new Set(items.map((item) => item.id)) : new Set())}
            />
          </div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--name">{t('名称')}</div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--size">{t('大小')}</div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--source">{t('来源')}</div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--time">{t('最近更新')}</div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--actions" />
        </div>
        {folderRows.map((folder) => {
          const menuItems: any[] = [];
          if (onEnterFolder) menuItems.push({ key: 'open', label: t('打开'), onClick: () => onEnterFolder(folder.folder_id) });
          if (onRenameFolder) menuItems.push({ key: 'rename', label: t('重命名'), onClick: () => onRenameFolder(folder.folder_id, folder.name) });
          if (onDeleteFolder) {
            if (menuItems.length > 0) menuItems.push({ type: 'divider' });
            menuItems.push({
              key: 'delete',
              label: <span style={{ color: 'var(--color-error)' }}>{t('删除文件夹')}</span>,
              onClick: () => onDeleteFolder(folder.folder_id, folder.name),
            });
          }
          return (
            <div
              key={folder.folder_id}
              className="jx-mySpace-docRow jx-mySpace-docRow--folder"
              onDoubleClick={() => onEnterFolder?.(folder.folder_id)}
              title={t('双击打开 {name}', { name: folder.name })}
            >
              <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--check" />
              <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--name">
                <img className="jx-mySpace-docRow-icon" src={getFolderIconSrc()} alt="" />
                <span className="jx-mySpace-docRow-name">{folder.name}</span>
              </div>
              <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--size">—</div>
              <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--source">{t('文件夹')}</div>
              <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--time">
                {folder.created_at ? new Date(folder.created_at).toLocaleDateString() : ''}
              </div>
              <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--actions">
                {menuItems.length > 0 && (
                  <Dropdown menu={{ items: menuItems }} trigger={['click']}>
                    <button type="button" className="jx-mySpace-moreBtn"><MoreOutlined /></button>
                  </Dropdown>
                )}
              </div>
            </div>
          );
        })}
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
                anySelected={someSelected}
                onCheck={(checked) => handleSelect(item.id, checked)}
                onDownload={onDownload}
                onNavigate={onNavigate}
                onDelete={onDelete}
                onPreview={onPreview}
                onAddToKb={onAddToKb ? (resource) => onAddToKb([resource]) : undefined}
                onMoveToPersonalFolder={onMoveToPersonalFolder}
                onCopyToPersonalFolder={onCopyToPersonalFolder}
              />
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
      <BulkActionBar open={someSelected} count={selectedItems.length}>
        {onAddToKb && (
          <button type="button" className="jx-mySpace-bulkBar-btn" onClick={() => onAddToKb(selectedItems.filter((item) => item.file_id))}>
            {t('加入知识库')}
          </button>
        )}
        {onBulkMoveToPersonalFolder && (
          <button type="button" className="jx-mySpace-bulkBar-btn" onClick={() => onBulkMoveToPersonalFolder(selectedItems)}>
            {t('移动到文件夹')}
          </button>
        )}
        {onBulkCopyToPersonalFolder && (
          <button type="button" className="jx-mySpace-bulkBar-btn" onClick={() => onBulkCopyToPersonalFolder(selectedItems)}>
            {t('复制到文件夹')}
          </button>
        )}
        <button type="button" className="jx-mySpace-bulkBar-btn" onClick={() => selectedItems.forEach(onDownload)}>
          {t('下载')}
        </button>
        {onDelete && (
          <button type="button" className="jx-mySpace-bulkBar-btn jx-mySpace-bulkBar-btn--danger" onClick={handleBulkDelete}>
            {t('删除')}
          </button>
        )}
        <div className="jx-mySpace-bulkBar-divider" />
        <button type="button" className="jx-mySpace-bulkBar-btn jx-mySpace-bulkBar-btn--cancel" onClick={() => setSelectedIds(new Set())}>
          {t('取消')}
        </button>
      </BulkActionBar>
    </>
  );
}
