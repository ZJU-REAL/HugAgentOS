import { useState, useEffect, useCallback, useMemo } from 'react';
import type { ReactNode } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Modal, Tabs, Checkbox, Input, Empty, Button, TreeSelect } from 'antd';
import { EASE } from '../../utils/motionTokens';
import { t } from '../../i18n';
import { SearchOutlined, FileOutlined, PictureOutlined, TeamOutlined, FolderOutlined, UserOutlined } from '@ant-design/icons';
import { FilePreviewPane } from './FilePreviewPane';

interface ScopeTreeNode {
  value: string;
  title: string;
  icon?: ReactNode;
  children?: ScopeTreeNode[];
}
import {
  getArtifacts,
  getApiUrl,
  listMyTeamsWithPermissions,
  listTeamFolderTree,
  listTeamFiles,
  listPersonalFolderTree,
} from '../../api';
import { useFileStore } from '../../stores';
import { useDelayedFlag } from '../../hooks';
import type { PersonalFolderNode, ResourceItem } from '../../types';
import type { ImportedSpaceFile } from '../../stores/fileStore';
import type { MyTeamItem, TeamFolderNode } from '../../types/teamFiles';
import { getFileIconSrc } from '../../utils/fileIcon';
import { formatDateKey } from '../../utils/date';

/** Project reference selection result —— the onSubmit argument when mode='project' */
export interface ProjectImportSelection {
  /** List of selected individual artifact IDs (MySpace personal + team mixed) */
  artifactIds: string[];
  /** List of selected whole personal folder IDs */
  folderIds: string[];
  /** List of selected whole team folder IDs */
  teamFolderIds: string[];
}

interface MySpaceImportModalProps {
  open: boolean;
  onClose: () => void;
  /**
   * 'attach' (default) = goes through the chat attachment flow (writes fileStore.addImportedSpaceFiles).
   * 'project' = project reference flow: passes the selection back to the parent via callback, and the parent calls
   * ``/v1/projects/{id}/files/reference``.
   */
  mode?: 'attach' | 'project';
  /** Provided by the parent when mode='project', handles artifact / folder references */
  onProjectSubmit?: (selection: ProjectImportSelection) => Promise<void> | void;
  /** Custom modal title (defaults: mode='attach' uses "从我的空间导入", 'project' uses "从我的空间引用") */
  title?: string;
}

type ImportScope =
  | { kind: 'personal'; folderId: string | null }
  | { kind: 'team'; teamId: string; folderId: string | null };

const PERSONAL_ROOT_KEY = 'personal::__root__';
const PERSONAL_FOLDER_KEY = (folderId: string) => `personal::${folderId}`;
const TEAM_ROOT_KEY = (teamId: string) => `team::${teamId}::__root__`;
const FOLDER_KEY = (teamId: string, folderId: string) => `team::${teamId}::${folderId}`;

function keyOfScope(s: ImportScope): string {
  if (s.kind === 'personal') {
    return s.folderId ? PERSONAL_FOLDER_KEY(s.folderId) : PERSONAL_ROOT_KEY;
  }
  return s.folderId ? FOLDER_KEY(s.teamId, s.folderId) : TEAM_ROOT_KEY(s.teamId);
}

function parseScopeKey(key: string): ImportScope | null {
  // Compatible with the old PERSONAL_KEY = 'personal'
  if (key === 'personal' || key === PERSONAL_ROOT_KEY) {
    return { kind: 'personal', folderId: null };
  }
  if (key.startsWith('personal::')) {
    const folderIdRaw = key.slice('personal::'.length);
    return { kind: 'personal', folderId: folderIdRaw === '__root__' ? null : folderIdRaw };
  }
  const parts = key.split('::');
  if (parts[0] !== 'team' || parts.length < 3) return null;
  const teamId = parts[1];
  const folderIdRaw = parts[2];
  return { kind: 'team', teamId, folderId: folderIdRaw === '__root__' ? null : folderIdRaw };
}

const IMAGE_MIMES = new Set(['image/png', 'image/jpeg', 'image/jpg', 'image/gif', 'image/webp', 'image/bmp', 'image/svg+xml']);

function isImageItem(item: ResourceItem) {
  return item.type === 'image' || (item.mime_type ? IMAGE_MIMES.has(item.mime_type) : false);
}

function formatSize(bytes?: number) {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

interface FileListProps {
  items: ResourceItem[];
  selected: Set<string>;
  previewId: string | null;
  onToggle: (id: string) => void;
  onPreview: (item: ResourceItem) => void;
}

function FileListSkeleton({ count = 5 }: { count?: number }) {
  return (
    <div className="jx-spaceImportList" aria-hidden="true">
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

function FileList({ items, selected, previewId, onToggle, onPreview }: FileListProps) {
  if (items.length === 0) {
    return <Empty description={t('暂无文件')} image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ margin: '32px 0' }} />;
  }
  return (
    <div className="jx-spaceImportList">
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
              {isImageItem(item) ? (
                (item.download_url || item.file_id) ? (
                  <img src={`${getApiUrl()}${item.download_url || `/files/${item.file_id}`}`} alt={item.name} className="jx-spaceImportItem-thumb" />
                ) : (
                  <PictureOutlined style={{ fontSize: 22, color: 'var(--color-primary)' }} />
                )
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
        );
      })}
    </div>
  );
}

interface FolderTreeLike {
  folder_id: string;
  name: string;
  children?: FolderTreeLike[];
}

function nodesToOptions(
  folders: FolderTreeLike[],
  buildKey: (folderId: string) => string,
): ScopeTreeNode[] {
  return folders.map((f) => ({
    value: buildKey(f.folder_id),
    title: f.name,
    icon: <FolderOutlined />,
    children: f.children?.length ? nodesToOptions(f.children, buildKey) : undefined,
  }));
}

export function MySpaceImportModal({
  open,
  onClose,
  mode = 'attach',
  onProjectSubmit,
  title,
}: MySpaceImportModalProps) {
  const [scope, setScope] = useState<ImportScope>({ kind: 'personal', folderId: null });
  const [allItems, setAllItems] = useState<ResourceItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [keyword, setKeyword] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  /** mode='project': the set of keys for selected "whole folders" —— key is the same as keyOfScope */
  const [selectedFolderKeys, setSelectedFolderKeys] = useState<Set<string>>(new Set());
  const [activeTab, setActiveTab] = useState<'all' | 'document' | 'image'>('all');
  const [myTeams, setMyTeams] = useState<MyTeamItem[]>([]);
  const [folderTreesByTeam, setFolderTreesByTeam] = useState<Record<string, TeamFolderNode[]>>({});
  const [personalTree, setPersonalTree] = useState<PersonalFolderNode[]>([]);
  const [previewItem, setPreviewItem] = useState<ResourceItem | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const { addImportedSpaceFiles } = useFileStore();

  const fetchItems = useCallback(async (s: ImportScope) => {
    setLoading(true);
    try {
      if (s.kind === 'personal') {
        const res = await getArtifacts({
          scope: 'personal',
          folder_id: s.folderId ?? '__root__',
          page: 1,
          page_size: 100,
        });
        setAllItems(res.items || []);
      } else {
        const res = await listTeamFiles({
          teamId: s.teamId,
          folderId: s.folderId,
          page: 1,
          page_size: 100,
        });
        setAllItems(res.items || []);
      }
    } catch (e) {
      console.error('Failed to load import files:', e);
      setAllItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  // First open: reset state + load team list + personal folder tree + fetch personal files
  useEffect(() => {
    if (!open) return;
    setSelected(new Set());
    setSelectedFolderKeys(new Set());
    setKeyword('');
    setActiveTab('all');
    setPreviewItem(null);
    const initial: ImportScope = { kind: 'personal', folderId: null };
    setScope(initial);
    void fetchItems(initial);
    void (async () => {
      try {
        const teams = await listMyTeamsWithPermissions();
        setMyTeams(teams);
      } catch (e) {
        console.error('Failed to load my teams for import:', e);
      }
    })();
    void (async () => {
      try {
        const tree = await listPersonalFolderTree();
        setPersonalTree(tree);
      } catch (e) {
        console.error('Failed to load personal folder tree for import:', e);
      }
    })();
  }, [open, fetchItems]);

  // Lazy-load the folder tree when scope switches to a team
  useEffect(() => {
    if (scope.kind !== 'team') return;
    if (folderTreesByTeam[scope.teamId]) return;
    void (async () => {
      try {
        const tree = await listTeamFolderTree(scope.teamId);
        setFolderTreesByTeam((prev) => ({ ...prev, [scope.teamId]: tree }));
      } catch (e) {
        console.error('Failed to load team folder tree:', e);
      }
    })();
  }, [scope, folderTreesByTeam]);

  const handleScopeChange = (key: string) => {
    const next = parseScopeKey(key);
    if (!next) return;
    setScope(next);
    setSelected(new Set());
    setKeyword('');
    setPreviewItem(null);
    void fetchItems(next);
  };

  const filteredItems = allItems.filter((item) => {
    if (keyword && !item.name.toLowerCase().includes(keyword.toLowerCase())) return false;
    if (activeTab === 'document') return !isImageItem(item);
    if (activeTab === 'image') return isImageItem(item);
    return true;
  });

  const toggleItem = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const resolveDownloadUrl = (item: ResourceItem) => {
    return item.download_url || (item.file_id ? `/files/${item.file_id}` : '');
  };

  const handleConfirm = async () => {
    if (mode === 'project') {
      const artifactIds = allItems
        .filter((item) => selected.has(item.id) && item.file_id)
        .map((item) => item.file_id!);
      const folderIds: string[] = [];
      const teamFolderIds: string[] = [];
      selectedFolderKeys.forEach((key) => {
        const s = parseScopeKey(key);
        if (!s || !s.folderId) return;
        if (s.kind === 'personal') folderIds.push(s.folderId);
        else teamFolderIds.push(s.folderId);
      });
      if (artifactIds.length === 0 && folderIds.length === 0 && teamFolderIds.length === 0) {
        onClose();
        return;
      }
      setSubmitting(true);
      try {
        await onProjectSubmit?.({ artifactIds, folderIds, teamFolderIds });
        onClose();
      } finally {
        setSubmitting(false);
      }
      return;
    }
    const toImport: ImportedSpaceFile[] = allItems
      .filter((item) => selected.has(item.id) && item.file_id)
      .map((item) => ({
        name: item.name,
        file_id: item.file_id!,
        download_url: resolveDownloadUrl(item),
        mime_type: item.mime_type || (isImageItem(item) ? 'image/png' : 'application/octet-stream'),
        type: isImageItem(item) ? 'image' : 'document',
      }));
    if (toImport.length > 0) {
      addImportedSpaceFiles(toImport);
    }
    onClose();
  };

  /** The current folder's "reference all" toggle shown when mode='project':
   *  available only when scope points to a specific folder (folderId is not null).
   */
  const currentFolderKey =
    mode === 'project' && scope.folderId
      ? (scope.kind === 'personal'
          ? PERSONAL_FOLDER_KEY(scope.folderId)
          : FOLDER_KEY(scope.teamId, scope.folderId))
      : null;
  const currentFolderSelected = currentFolderKey ? selectedFolderKeys.has(currentFolderKey) : false;
  const toggleCurrentFolder = () => {
    if (!currentFolderKey) return;
    setSelectedFolderKeys((prev) => {
      const next = new Set(prev);
      if (next.has(currentFolderKey)) next.delete(currentFolderKey);
      else next.add(currentFolderKey);
      return next;
    });
  };

  const docCount = allItems.filter((i) => !isImageItem(i)).length;
  const imgCount = allItems.filter((i) => isImageItem(i)).length;

  const scopeTreeData: ScopeTreeNode[] = useMemo(() => {
    const nodes: ScopeTreeNode[] = [
      {
        value: PERSONAL_ROOT_KEY,
        title: t('我的空间'),
        icon: <UserOutlined />,
        children: personalTree.length > 0 ? nodesToOptions(personalTree, PERSONAL_FOLDER_KEY) : undefined,
      },
    ];
    myTeams.forEach((team) => {
      const tree = folderTreesByTeam[team.team_id] || [];
      nodes.push({
        value: TEAM_ROOT_KEY(team.team_id),
        title: team.name,
        icon: <TeamOutlined />,
        children: tree.length > 0 ? nodesToOptions(tree, (fid) => FOLDER_KEY(team.team_id, fid)) : undefined,
      });
    });
    return nodes;
  }, [myTeams, folderTreesByTeam, personalTree]);

  const scopeKey = keyOfScope(scope);
  const showSkeleton = useDelayedFlag(loading);

  const totalSelected = selected.size + (mode === 'project' ? selectedFolderKeys.size : 0);
  const confirmLabel = mode === 'project' ? t('确认引用') : t('确认导入');
  const summaryText = mode === 'project'
    ? (totalSelected > 0
        ? (selectedFolderKeys.size
            ? t('已选 {files} 个文件 + {folders} 个文件夹', { files: selected.size, folders: selectedFolderKeys.size })
            : t('已选 {n} 个文件', { n: selected.size }))
        : t('请选择要引用的文件或文件夹'))
    : (totalSelected > 0 ? t('已选 {n} 个文件', { n: selected.size }) : t('请选择要导入的文件'));

  return (
    <Modal
      title={title || (mode === 'project' ? t('从我的空间引用') : t('从我的空间导入'))}
      open={open}
      onCancel={onClose}
      width={1080}
      footer={
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ fontSize: 13, color: 'var(--color-text-tertiary)' }}>
            {summaryText}
          </span>
          <div style={{ display: 'flex', gap: 8 }}>
            <Button onClick={onClose} disabled={submitting}>{t('取消')}</Button>
            <Button
              type="primary"
              onClick={handleConfirm}
              disabled={totalSelected === 0 || submitting}
              loading={submitting}
            >
              {confirmLabel}
            </Button>
          </div>
        </div>
      }
      className="jx-spaceImportModal"
      destroyOnClose
    >
      <div className="jx-spaceImportLayout">
        <div className="jx-spaceImportLeft">
          <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
            <TreeSelect
              style={{ width: 200 }}
              value={scopeKey}
              onChange={handleScopeChange}
              treeData={scopeTreeData}
              treeDefaultExpandAll
              treeLine
              showSearch
              treeNodeFilterProp="title"
              placeholder={t('选择来源')}
              onTreeExpand={(keys) => {
                // Lazy-load: fetch the folder tree of a team root node when it's expanded
                keys.forEach((k) => {
                  const key = String(k);
                  if (key.endsWith('::__root__') && key.startsWith('team::')) {
                    const teamId = key.split('::')[1];
                    if (!folderTreesByTeam[teamId]) {
                      void listTeamFolderTree(teamId).then((tree) => {
                        setFolderTreesByTeam((prev) => ({ ...prev, [teamId]: tree }));
                      });
                    }
                  }
                });
              }}
            />
            <Input
              placeholder={t('搜索文件名')}
              prefix={<SearchOutlined />}
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              allowClear
            />
          </div>
          {/* "Reference the whole folder" hint bar: expands/collapses with height-auto as folders switch */}
          <AnimatePresence initial={false}>
            {mode === 'project' && currentFolderKey && (
              <motion.div
                initial={{ height: 0, opacity: 0, marginBottom: 0 }}
                animate={{ height: 'auto', opacity: 1, marginBottom: 8 }}
                exit={{ height: 0, opacity: 0, marginBottom: 0 }}
                transition={{ duration: 0.2, ease: EASE.standard }}
                style={{ overflow: 'hidden', flexShrink: 0 }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    padding: '6px 10px',
                    background: 'var(--color-primary-light, #EBF2FF)',
                    borderRadius: 6,
                    fontSize: 13,
                  }}
                >
                  <Checkbox checked={currentFolderSelected} onChange={toggleCurrentFolder}>
                    <FolderOutlined style={{ marginRight: 4 }} />
                    {t('引用整个当前文件夹（含子文件夹下全部文件）')}
                  </Checkbox>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
          <Tabs
            activeKey={activeTab}
            onChange={(k) => setActiveTab(k as 'all' | 'document' | 'image')}
            size="small"
            items={[
              {
                key: 'all',
                label: `${t('全部')} (${allItems.length})`,
              },
              {
                key: 'document',
                label: <span><FileOutlined style={{ marginRight: 4 }} />{t('文档')} ({docCount})</span>,
              },
              {
                key: 'image',
                label: <span><PictureOutlined style={{ marginRight: 4 }} />{t('图片')} ({imgCount})</span>,
              },
            ]}
          />
          {showSkeleton ? (
            <FileListSkeleton />
          ) : loading ? null : (
            <FileList
              items={filteredItems}
              selected={selected}
              previewId={previewItem?.id ?? null}
              onToggle={toggleItem}
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
