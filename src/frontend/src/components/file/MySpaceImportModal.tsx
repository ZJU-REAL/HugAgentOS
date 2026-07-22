import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  FileOutlined,
  FolderOutlined,
  PictureOutlined,
  SearchOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Button, Checkbox, Empty, Input, Modal, Tabs, TreeSelect } from 'antd';
import { AnimatePresence, motion } from 'motion/react';

import { getApiUrl, getArtifacts, listPersonalFolderTree } from '../../api';
import { useDelayedFlag } from '../../hooks';
import { t } from '../../i18n';
import { useFileStore } from '../../stores';
import type { ImportedSpaceFile } from '../../stores/fileStore';
import type { PersonalFolderNode, ResourceItem } from '../../types';
import { formatDateKey } from '../../utils/date';
import { getFileIconSrc } from '../../utils/fileIcon';
import { EASE } from '../../utils/motionTokens';
import { FilePreviewPane } from './FilePreviewPane';

interface ScopeTreeNode {
  value: string;
  title: string;
  icon?: ReactNode;
  children?: ScopeTreeNode[];
}

export interface ProjectImportSelection {
  artifactIds: string[];
  folderIds: string[];
}

interface MySpaceImportModalProps {
  open: boolean;
  onClose: () => void;
  mode?: 'attach' | 'project';
  onProjectSubmit?: (selection: ProjectImportSelection) => Promise<void> | void;
  title?: string;
}

const ROOT_KEY = 'personal::__root__';
const folderKey = (folderId: string) => `personal::${folderId}`;

function parseFolderKey(key: string): string | null | undefined {
  if (key === ROOT_KEY || key === 'personal') return null;
  if (!key.startsWith('personal::')) return undefined;
  const folderId = key.slice('personal::'.length);
  return folderId && folderId !== '__root__' ? folderId : null;
}

const IMAGE_MIMES = new Set([
  'image/png',
  'image/jpeg',
  'image/jpg',
  'image/gif',
  'image/webp',
  'image/bmp',
  'image/svg+xml',
]);

function isImageItem(item: ResourceItem): boolean {
  return item.type === 'image' || (!!item.mime_type && IMAGE_MIMES.has(item.mime_type));
}

function formatSize(bytes?: number): string {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function foldersToOptions(folders: PersonalFolderNode[]): ScopeTreeNode[] {
  return folders.map((folder) => ({
    value: folderKey(folder.folder_id),
    title: folder.name,
    icon: <FolderOutlined />,
    children: folder.children?.length ? foldersToOptions(folder.children) : undefined,
  }));
}

function FileListSkeleton() {
  return (
    <div className="jx-spaceImportList" aria-hidden="true">
      {Array.from({ length: 5 }).map((_, index) => (
        <div key={index} className="jx-spaceImportItem jx-spaceImportItem--skeleton">
          <div className="jx-skeletonBlock jx-spaceImportItem-skCheckbox" />
          <div className="jx-spaceImportItem-icon">
            <div className="jx-skeletonBlock jx-spaceImportItem-skIcon" />
          </div>
          <div className="jx-spaceImportItem-info">
            <div className="jx-skeletonBlock jx-spaceImportItem-skName" />
            <div className="jx-spaceImportItem-meta">
              <div className="jx-skeletonBlock jx-spaceImportItem-skMeta" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function SelectableFileList({
  items,
  selected,
  previewId,
  onToggle,
  onPreview,
}: {
  items: ResourceItem[];
  selected: Set<string>;
  previewId: string | null;
  onToggle: (id: string) => void;
  onPreview: (item: ResourceItem) => void;
}) {
  if (items.length === 0) {
    return <Empty description={t('暂无文件')} image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ margin: '32px 0' }} />;
  }
  return (
    <div className="jx-spaceImportList">
      {items.map((item) => (
        <div
          key={item.id}
          className={`jx-spaceImportItem${selected.has(item.id) ? ' selected' : ''}${previewId === item.id ? ' previewing' : ''}`}
          onClick={() => onPreview(item)}
        >
          <Checkbox
            checked={selected.has(item.id)}
            onChange={() => onToggle(item.id)}
            onClick={(event) => event.stopPropagation()}
          />
          <div className="jx-spaceImportItem-icon">
            {isImageItem(item) && (item.download_url || item.file_id) ? (
              <img
                src={`${getApiUrl()}${item.download_url || `/files/${item.file_id}`}`}
                alt={item.name}
                className="jx-spaceImportItem-thumb"
              />
            ) : (
              <img src={getFileIconSrc(item.name)} width={24} height={24} alt="" />
            )}
          </div>
          <div className="jx-spaceImportItem-info">
            <div className="jx-spaceImportItem-name" title={item.name}>{item.name}</div>
            <div className="jx-spaceImportItem-meta">
              {item.size ? <span>{formatSize(item.size)}</span> : null}
              <span>{formatDateKey(item.created_at)}</span>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export function MySpaceImportModal({
  open,
  onClose,
  mode = 'attach',
  onProjectSubmit,
  title,
}: MySpaceImportModalProps) {
  const [folderId, setFolderId] = useState<string | null>(null);
  const [items, setItems] = useState<ResourceItem[]>([]);
  const [folders, setFolders] = useState<PersonalFolderNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [keyword, setKeyword] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [selectedFolders, setSelectedFolders] = useState<Set<string>>(new Set());
  const [activeTab, setActiveTab] = useState<'all' | 'document' | 'image'>('all');
  const [previewItem, setPreviewItem] = useState<ResourceItem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const addImportedSpaceFiles = useFileStore((state) => state.addImportedSpaceFiles);

  const fetchItems = useCallback(async (nextFolderId: string | null) => {
    setLoading(true);
    try {
      const result = await getArtifacts({
        scope: 'personal',
        folder_id: nextFolderId ?? '__root__',
        page: 1,
        page_size: 100,
      });
      setItems(result.items ?? []);
    } catch (error) {
      console.error('Failed to load personal import files:', error);
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setFolderId(null);
    setKeyword('');
    setSelected(new Set());
    setSelectedFolders(new Set());
    setActiveTab('all');
    setPreviewItem(null);
    void fetchItems(null);
    void listPersonalFolderTree().then(setFolders).catch(() => setFolders([]));
  }, [fetchItems, open]);

  const filteredItems = items.filter((item) => {
    if (keyword && !item.name.toLowerCase().includes(keyword.toLowerCase())) return false;
    if (activeTab === 'document') return !isImageItem(item);
    if (activeTab === 'image') return isImageItem(item);
    return true;
  });

  const scopeTree = useMemo<ScopeTreeNode[]>(() => [{
    value: ROOT_KEY,
    title: t('我的空间'),
    icon: <UserOutlined />,
    children: folders.length > 0 ? foldersToOptions(folders) : undefined,
  }], [folders]);

  const currentFolderKey = mode === 'project' && folderId ? folderKey(folderId) : null;
  const totalSelected = selected.size + (mode === 'project' ? selectedFolders.size : 0);
  const docCount = items.filter((item) => !isImageItem(item)).length;
  const imageCount = items.filter(isImageItem).length;
  const showSkeleton = useDelayedFlag(loading);

  const handleConfirm = async () => {
    if (mode === 'project') {
      const artifactIds = items
        .filter((item) => selected.has(item.id) && item.file_id)
        .map((item) => item.file_id as string);
      const folderIds = Array.from(selectedFolders)
        .map(parseFolderKey)
        .filter((value): value is string => typeof value === 'string' && value.length > 0);
      if (artifactIds.length === 0 && folderIds.length === 0) {
        onClose();
        return;
      }
      setSubmitting(true);
      try {
        await onProjectSubmit?.({ artifactIds, folderIds });
        onClose();
      } finally {
        setSubmitting(false);
      }
      return;
    }

    const imports: ImportedSpaceFile[] = items
      .filter((item) => selected.has(item.id) && item.file_id)
      .map((item) => ({
        name: item.name,
        file_id: item.file_id as string,
        download_url: item.download_url || `/files/${item.file_id}`,
        mime_type: item.mime_type || (isImageItem(item) ? 'image/png' : 'application/octet-stream'),
        type: isImageItem(item) ? 'image' : 'document',
      }));
    if (imports.length > 0) addImportedSpaceFiles(imports);
    onClose();
  };

  const summary = mode === 'project'
    ? (totalSelected > 0
        ? t('已选 {files} 个文件 + {folders} 个文件夹', {
            files: selected.size,
            folders: selectedFolders.size,
          })
        : t('请选择要引用的文件或文件夹'))
    : (selected.size > 0 ? t('已选 {n} 个文件', { n: selected.size }) : t('请选择要导入的文件'));

  return (
    <Modal
      title={title || (mode === 'project' ? t('从我的空间引用') : t('从我的空间导入'))}
      open={open}
      onCancel={onClose}
      width={1080}
      footer={(
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ fontSize: 13, color: 'var(--color-text-tertiary)' }}>{summary}</span>
          <div style={{ display: 'flex', gap: 8 }}>
            <Button onClick={onClose} disabled={submitting}>{t('取消')}</Button>
            <Button type="primary" onClick={() => void handleConfirm()} disabled={totalSelected === 0 || submitting} loading={submitting}>
              {mode === 'project' ? t('确认引用') : t('确认导入')}
            </Button>
          </div>
        </div>
      )}
      className="jx-spaceImportModal"
      destroyOnClose
    >
      <div className="jx-spaceImportLayout">
        <div className="jx-spaceImportLeft">
          <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
            <TreeSelect
              style={{ width: 200 }}
              value={folderId ? folderKey(folderId) : ROOT_KEY}
              onChange={(key) => {
                const nextFolderId = parseFolderKey(String(key));
                if (nextFolderId === undefined) return;
                setFolderId(nextFolderId);
                setSelected(new Set());
                setKeyword('');
                setPreviewItem(null);
                void fetchItems(nextFolderId);
              }}
              treeData={scopeTree}
              treeDefaultExpandAll
              treeLine
              showSearch
              treeNodeFilterProp="title"
              placeholder={t('选择来源')}
            />
            <Input
              placeholder={t('搜索文件名')}
              prefix={<SearchOutlined />}
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              allowClear
            />
          </div>
          <AnimatePresence initial={false}>
            {currentFolderKey && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2, ease: EASE.standard }}
                style={{ overflow: 'hidden', marginBottom: 8 }}
              >
                <Checkbox
                  checked={selectedFolders.has(currentFolderKey)}
                  onChange={() => setSelectedFolders((previous) => {
                    const next = new Set(previous);
                    if (next.has(currentFolderKey)) next.delete(currentFolderKey);
                    else next.add(currentFolderKey);
                    return next;
                  })}
                >
                  <FolderOutlined style={{ marginRight: 4 }} />
                  {t('引用整个当前文件夹（含子文件夹下全部文件）')}
                </Checkbox>
              </motion.div>
            )}
          </AnimatePresence>
          <Tabs
            activeKey={activeTab}
            onChange={(key) => setActiveTab(key as 'all' | 'document' | 'image')}
            size="small"
            items={[
              { key: 'all', label: `${t('全部')} (${items.length})` },
              { key: 'document', label: <span><FileOutlined /> {t('文档')} ({docCount})</span> },
              { key: 'image', label: <span><PictureOutlined /> {t('图片')} ({imageCount})</span> },
            ]}
          />
          {showSkeleton ? <FileListSkeleton /> : loading ? null : (
            <SelectableFileList
              items={filteredItems}
              selected={selected}
              previewId={previewItem?.id ?? null}
              onToggle={(id) => setSelected((previous) => {
                const next = new Set(previous);
                if (next.has(id)) next.delete(id);
                else next.add(id);
                return next;
              })}
              onPreview={setPreviewItem}
            />
          )}
        </div>
        <div className="jx-spaceImportRight">
          <FilePreviewPane item={previewItem} />
        </div>
      </div>
    </Modal>
  );
}
