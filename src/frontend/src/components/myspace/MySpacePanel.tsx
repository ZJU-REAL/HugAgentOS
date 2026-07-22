import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import type { Variants } from 'motion/react';
import {
  ArrowLeftOutlined,
  DownOutlined,
  FileOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
  PictureOutlined,
  SearchOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { Button, Dropdown, Input, message, Modal, Select } from 'antd';

import {
  addArtifactToKnowledgeBase,
  createPersonalFolder,
  getAutomationRuns,
  uploadFile,
} from '../../api';
import { useDelayedFlag } from '../../hooks';
import { useFileDropZone } from '../../hooks/useFileDropZone';
import { t } from '../../i18n';
import {
  useAutomationChatStore,
  useCanvasStore,
  useCatalogStore,
  useChatStore,
} from '../../stores';
import { useMySpaceStore } from '../../stores/mySpaceStore';
import type { KBItem, MySpaceTab, ResourceItem } from '../../types';
import { buildFileUrl } from '../../utils/constants';
import { findFolderById } from '../../utils/folderTree';
import { EASE, SPRING } from '../../utils/motionTokens';
import { DropOverlay } from '../common/DropOverlay';
import type { UploadProgress } from '../common/UploadProgressBar';
import { UploadProgressBar } from '../common/UploadProgressBar';
import { ShareRecordsPage } from '../share';
import { DocumentList } from './DocumentList';
import { FavoriteList } from './FavoriteList';
import { ImageGrid } from './ImageGrid';
import { MySpaceSkeleton } from './MySpaceSkeleton';
import { NotificationList } from './NotificationList';
import {
  CreatePersonalFolderModal,
  MoveToPersonalFolderModal,
} from './personal';

const TABS: Array<{ key: MySpaceTab; label: string }> = [
  { key: 'assets', label: t('文件资产') },
  { key: 'favorites', label: t('会话收藏') },
  { key: 'shares', label: t('分享记录') },
  { key: 'notifications', label: t('消息通知') },
];

const UPLOAD_CONCURRENCY = 4;

const scopeSlideVariants: Variants = {
  enter: (direction: number) => ({ opacity: 0, x: direction * 24 }),
  center: { opacity: 1, x: 0, transition: { duration: 0.22, ease: EASE.brandOut } },
  exit: (direction: number) => ({
    opacity: 0,
    x: direction * -24,
    transition: { duration: 0.16, ease: EASE.exit },
  }),
};

async function runWithConcurrency<T>(
  items: T[],
  limit: number,
  worker: (item: T) => Promise<void>,
): Promise<void> {
  const queue = items.slice();
  const consume = async () => {
    while (queue.length > 0) {
      const item = queue.shift();
      if (item === undefined) return;
      await worker(item);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, consume));
}

export function MySpacePanel() {
  const enterAutomationChat = useAutomationChatStore((state) => state.enterAutomationChat);
  const exitAutomationChat = useAutomationChatStore((state) => state.exitAutomationChat);
  const {
    resources,
    favorites,
    loading,
    tab,
    searchKeyword,
    hasMore,
    favHasMore,
    assetFilter,
    sourceFilter,
    notifUnreadCount,
    selectedScope,
    personalChildFolders,
    personalFolderTree,
    setTab,
    setSearchKeyword,
    setAssetFilter,
    setSourceFilter,
    fetchResources,
    fetchFavorites,
    deleteResource,
    unfavoriteChat,
    removeFavorite,
    loadMore,
    loadPersonalFolderTree,
    enterPersonalFolder,
    renamePersonalFolderAction,
    deletePersonalFolderAction,
    uploadPersonalFile,
  } = useMySpaceStore();
  const { catalog, setPanel } = useCatalogStore();
  const setCurrentChatId = useChatStore((state) => state.setCurrentChatId);
  const openCanvas = useCanvasStore((state) => state.openCanvas);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const navDirection = useRef(0);
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(null);
  const [createFolderOpen, setCreateFolderOpen] = useState(false);
  const [moveFolderOpen, setMoveFolderOpen] = useState(false);
  const [moveArtifactIds, setMoveArtifactIds] = useState<string[]>([]);
  const [moveMode, setMoveMode] = useState<'move' | 'copy'>('move');
  const [kbPickerOpen, setKbPickerOpen] = useState(false);
  const [kbPickerLoading, setKbPickerLoading] = useState(false);
  const [selectedKbIds, setSelectedKbIds] = useState<string[]>([]);
  const [pendingResources, setPendingResources] = useState<ResourceItem[]>([]);

  const parentFolderId = useMemo<string | null>(() => {
    if (!selectedScope.folderId) return null;
    return findFolderById(personalFolderTree, selectedScope.folderId)?.parent_folder_id ?? null;
  }, [personalFolderTree, selectedScope.folderId]);

  useEffect(() => {
    void loadPersonalFolderTree();
  }, [loadPersonalFolderTree]);

  useEffect(() => {
    if (tab === 'favorites') void fetchFavorites(true);
    if (tab === 'assets') void fetchResources(true);
    // The initial tab is intentionally loaded once; the store handles later switches.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => () => {
    if (searchTimer.current) clearTimeout(searchTimer.current);
  }, []);

  const handleSearch = useCallback((value: string) => {
    setSearchKeyword(value);
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => {
      if (tab === 'favorites') void fetchFavorites(true);
      if (tab === 'assets') void fetchResources(true);
    }, 300);
  }, [fetchFavorites, fetchResources, setSearchKeyword, tab]);

  const handleFilesPicked = useCallback(async (files: FileList | null) => {
    if (!files?.length) return;
    const items = Array.from(files);
    let completed = 0;
    let failed = 0;
    setUploadProgress({ done: 0, total: items.length });
    await runWithConcurrency(items, UPLOAD_CONCURRENCY, async (file) => {
      try {
        await uploadPersonalFile(file);
      } catch (error) {
        failed += 1;
        message.error(`${file.name}: ${(error as Error)?.message || t('上传失败')}`);
      }
      completed += 1;
      setUploadProgress({ done: completed, total: items.length });
    });
    setUploadProgress(null);
    if (items.length - failed > 0) {
      message.success(t('已上传 {n} 个文件', { n: items.length - failed }));
    }
  }, [uploadPersonalFile]);

  const handleFolderPicked = useCallback(async (files: FileList | null) => {
    const items = Array.from(files ?? []);
    if (items.length === 0) return;

    const directoriesByDepth = new Map<number, Set<string>>();
    const entries: Array<{ file: File; directory: string }> = [];
    items.forEach((file) => {
      const relativePath = (file as File & { webkitRelativePath?: string }).webkitRelativePath ?? '';
      const slash = relativePath.lastIndexOf('/');
      const directory = slash < 0 ? '' : relativePath.slice(0, slash);
      if (directory) {
        const segments = directory.split('/').filter(Boolean);
        let current = '';
        segments.forEach((segment, index) => {
          current = current ? `${current}/${segment}` : segment;
          if (!directoriesByDepth.has(index + 1)) directoriesByDepth.set(index + 1, new Set());
          directoriesByDepth.get(index + 1)?.add(current);
        });
      }
      entries.push({ file, directory });
    });

    const pathToFolderId = new Map<string, string | null>([['', selectedScope.folderId]]);
    let refreshedTree = personalFolderTree;
    const findChild = (parentId: string | null, name: string): string | null => {
      if (parentId === null) {
        return refreshedTree.find((folder) => folder.name === name)?.folder_id ?? null;
      }
      return findFolderById(refreshedTree, parentId)?.children
        ?.find((folder) => folder.name === name)?.folder_id ?? null;
    };

    setUploadProgress({ done: 0, total: entries.length });
    for (const depth of Array.from(directoriesByDepth.keys()).sort((left, right) => left - right)) {
      for (const directory of directoriesByDepth.get(depth) ?? []) {
        const slash = directory.lastIndexOf('/');
        const parentPath = slash < 0 ? '' : directory.slice(0, slash);
        const name = slash < 0 ? directory : directory.slice(slash + 1);
        const parentId = pathToFolderId.get(parentPath) ?? null;
        try {
          const created = await createPersonalFolder(name, parentId);
          pathToFolderId.set(directory, created.folder_id);
        } catch (error) {
          await loadPersonalFolderTree();
          refreshedTree = useMySpaceStore.getState().personalFolderTree;
          const existing = findChild(parentId, name);
          if (!existing) {
            setUploadProgress(null);
            message.error((error as Error)?.message || t('建文件夹失败：'));
            return;
          }
          pathToFolderId.set(directory, existing);
        }
      }
    }

    let completed = 0;
    let failed = 0;
    await runWithConcurrency(entries, UPLOAD_CONCURRENCY, async ({ file, directory }) => {
      try {
        await uploadFile(file, undefined, pathToFolderId.get(directory) ?? null);
      } catch {
        failed += 1;
      }
      completed += 1;
      setUploadProgress({ done: completed, total: entries.length });
    });
    setUploadProgress(null);
    if (failed > 0) {
      message.warning(t('上传完成：成功 {ok} 个，失败 {failed} 个', {
        ok: entries.length - failed,
        failed,
      }));
    } else {
      message.success(t('已上传 {n} 个文件', { n: entries.length }));
    }
    await loadPersonalFolderTree();
    await fetchResources(true);
  }, [fetchResources, loadPersonalFolderTree, personalFolderTree, selectedScope.folderId]);

  const handleRenameFolder = useCallback((folderId: string, currentName: string) => {
    let nextName = currentName;
    Modal.confirm({
      title: t('重命名文件夹'),
      icon: <FolderAddOutlined />,
      content: (
        <input
          className="jx-folder-input"
          autoFocus
          defaultValue={currentName}
          maxLength={255}
          onChange={(event) => { nextName = event.target.value; }}
        />
      ),
      okText: t('保存'),
      cancelText: t('取消'),
      onOk: async () => {
        const value = nextName.trim();
        if (!value) throw new Error(t('名称不能为空'));
        await renamePersonalFolderAction(folderId, value);
        message.success(t('已重命名'));
      },
    });
  }, [renamePersonalFolderAction]);

  const handleDeleteFolder = useCallback((folderId: string, name: string) => {
    Modal.confirm({
      title: t('删除文件夹"{name}"？', { name }),
      content: t('该文件夹及其所有子文件夹、文件都将被软删除（可在数据库中找回）。'),
      okText: t('删除'),
      okType: 'danger',
      cancelText: t('取消'),
      onOk: async () => {
        const affected = await deletePersonalFolderAction(folderId);
        message.success(affected > 0
          ? t('已删除文件夹及其下 {affected} 个文件', { affected })
          : t('文件夹已删除'));
      },
    });
  }, [deletePersonalFolderAction]);

  const openMoveModal = useCallback((ids: string[], mode: 'move' | 'copy') => {
    setMoveArtifactIds(ids);
    setMoveMode(mode);
    setMoveFolderOpen(true);
  }, []);

  const handleDownload = useCallback((item: ResourceItem) => {
    if (!item.file_id) return;
    const anchor = document.createElement('a');
    anchor.href = buildFileUrl(item.file_id);
    anchor.download = item.name;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
  }, []);

  const handleNavigate = useCallback((item: ResourceItem) => {
    if (!item.source_chat_id) return;
    if (item.source_chat_id.startsWith('automation:')) {
      const taskId = item.source_chat_id.slice('automation:'.length);
      void getAutomationRuns(taskId, 50)
        .then((runs) => enterAutomationChat(taskId, item.source_chat_title || item.name, runs))
        .catch((error) => message.error((error as Error)?.message || t('打开自动化任务失败')));
      return;
    }
    if (useAutomationChatStore.getState().activeGroup) exitAutomationChat();
    setCurrentChatId(item.source_chat_id);
    setPanel('chat');
  }, [enterAutomationChat, exitAutomationChat, setCurrentChatId, setPanel]);

  const handlePreview = useCallback((item: ResourceItem) => {
    if (!item.file_id) return;
    openCanvas({
      file_id: item.file_id,
      name: item.name,
      url: `/files/${item.file_id}`,
      mime_type: item.mime_type,
      size: item.size,
    });
  }, [openCanvas]);

  const handleRequestUnfavorite = useCallback(async (item: ResourceItem) => {
    if (!item.source_chat_id) return false;
    return await new Promise<boolean>((resolve) => {
      Modal.confirm({
        title: t('取消收藏'),
        content: t('确定将这条会话从收藏列表中移除吗？'),
        okText: t('取消收藏'),
        cancelText: t('保留'),
        onOk: async () => {
          try {
            await unfavoriteChat(item.source_chat_id as string);
            resolve(true);
          } catch (error) {
            message.error((error as Error)?.message || t('取消收藏失败'));
            resolve(false);
          }
        },
        onCancel: () => resolve(false),
      });
    });
  }, [unfavoriteChat]);

  const privateKbOptions = useMemo<KBItem[]>(
    () => catalog.kb.filter((item) => (
      item.visibility === 'private' && item.editable !== false && !item.system_managed
    )),
    [catalog.kb],
  );

  const openKbPicker = useCallback((items: ResourceItem | ResourceItem[]) => {
    const nextItems = (Array.isArray(items) ? items : [items]).filter((item) => !!item.file_id);
    if (privateKbOptions.length === 0) {
      message.warning(t('请先创建至少一个私有知识库'));
      return;
    }
    if (nextItems.length === 0) return;
    setPendingResources(nextItems);
    setSelectedKbIds(privateKbOptions[0]?.id ? [privateKbOptions[0].id] : []);
    setKbPickerOpen(true);
  }, [privateKbOptions]);

  const handleAddToKb = useCallback(async () => {
    if (pendingResources.length === 0 || selectedKbIds.length === 0) return;
    setKbPickerLoading(true);
    try {
      await Promise.all(pendingResources.flatMap((resource) => (
        selectedKbIds.map((kbId) => addArtifactToKnowledgeBase(resource.id, kbId))
      )));
      message.success(t('已将 {fileCount} 个文件加入 {kbCount} 个知识库，正在索引', {
        fileCount: pendingResources.length,
        kbCount: selectedKbIds.length,
      }));
      await fetchResources(true);
      setKbPickerOpen(false);
    } catch (error) {
      message.error((error as Error)?.message || t('加入知识库失败'));
    } finally {
      setKbPickerLoading(false);
    }
  }, [fetchResources, pendingResources, selectedKbIds]);

  const canDropUpload = tab === 'assets';
  const { dragActive, dropZoneProps } = useFileDropZone(canDropUpload, (files) => {
    void handleFilesPicked(files);
  });
  const showSkeleton = useDelayedFlag(loading && resources.length === 0);
  const currentItems = tab === 'favorites' ? favorites : resources;
  const currentHasMore = tab === 'favorites' ? favHasMore : hasMore;
  const tabDescriptions: Record<MySpaceTab, string> = {
    assets: t('汇集与AI会话过程中上传或生成的各类文档与图片，可按需加入你创建的私有知识库'),
    favorites: t('集中管理你收藏的重要会话与自动化任务，方便快速回看与继续交流'),
    shares: t('查看并管理已生成的分享链接与有效状态，查看浏览量'),
    notifications: t('查看自动化任务执行结果通知，及时了解任务完成状态'),
  };

  const renderEmpty = (text: string) => (
    <div className="jx-mySpace-empty">
      <div className="jx-mySpace-emptyIcon">
        <svg viewBox="0 0 48 48" width="48" height="48" fill="none">
          <rect x="8" y="6" width="32" height="36" rx="4" stroke="var(--color-fill-deep)" strokeWidth="2" />
          <path d="M16 18h16M16 26h10" stroke="var(--color-fill-deep)" strokeWidth="2" strokeLinecap="round" />
        </svg>
      </div>
      <div className="jx-mySpace-emptyText">{text}</div>
    </div>
  );

  return (
    <div className="jx-mySpace" {...dropZoneProps}>
      <div className="jx-mySpace-shell">
        <div className="jx-mySpace-header">
          <div className="jx-mySpace-tabs">
            {TABS.map((item) => (
              <button
                key={item.key}
                className={`jx-mySpace-tab${tab === item.key ? ' active' : ''}`}
                onClick={() => { navDirection.current = 0; setTab(item.key); }}
              >
                <span>{item.label}</span>
                {item.key === 'notifications' && notifUnreadCount > 0 && (
                  <motion.span className="jx-mySpace-tabBadge" initial={{ scale: 0.6 }} animate={{ scale: 1 }}>
                    {notifUnreadCount > 99 ? '99+' : notifUnreadCount}
                  </motion.span>
                )}
                {tab === item.key && (
                  <motion.span layoutId="jx-mySpace-tabInk" className="jx-mySpace-tabInk" transition={SPRING.ink} />
                )}
              </button>
            ))}
          </div>
          <div className={`jx-mySpace-subHeader${tab === 'shares' ? ' jx-mySpace-subHeader-shares' : ''}`}>
            <p className="jx-mySpace-desc">{tabDescriptions[tab]}</p>
          </div>

          {tab === 'assets' && (
            <>
              <div className="jx-mySpace-assetBar">
                <div className="jx-mySpace-filterTabs" role="tablist" aria-label={t('类型筛选')}>
                  {([
                    { key: 'document', label: t('文档') },
                    { key: 'image', label: t('图片') },
                  ] as const).map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      role="tab"
                      aria-selected={assetFilter === item.key}
                      className={`jx-mySpace-filterTab${assetFilter === item.key ? ' active' : ''}`}
                      onClick={() => { navDirection.current = 0; setAssetFilter(item.key); }}
                    >
                      {item.label}
                      {assetFilter === item.key && (
                        <motion.span layoutId="jx-mySpace-filterTabInk" className="jx-mySpace-tabInk jx-mySpace-tabInk--filter" transition={SPRING.ink} />
                      )}
                    </button>
                  ))}
                </div>
                <div className="jx-mySpace-filterTools">
                  {selectedScope.folderId !== null && (
                    <Button
                      type="text"
                      icon={<ArrowLeftOutlined />}
                      onClick={() => {
                        navDirection.current = -1;
                        void enterPersonalFolder(parentFolderId);
                      }}
                    >
                      {t('返回上级')}
                    </Button>
                  )}
                  {assetFilter === 'document' ? (
                    <>
                      <Button icon={<FolderAddOutlined />} onClick={() => setCreateFolderOpen(true)}>
                        {t('新建文件夹')}
                      </Button>
                      <Dropdown
                        trigger={['click']}
                        menu={{
                          items: [
                            { key: 'file', icon: <FileOutlined />, label: t('上传文件'), onClick: () => fileInputRef.current?.click() },
                            { key: 'folder', icon: <FolderOpenOutlined />, label: t('上传文件夹'), onClick: () => folderInputRef.current?.click() },
                          ],
                        }}
                      >
                        <Button type="primary" icon={<UploadOutlined />} loading={!!uploadProgress}>
                          {uploadProgress
                            ? t('上传中 {done}/{total}', {
                                done: uploadProgress.done,
                                total: uploadProgress.total,
                              })
                            : t('上传文件')}
                          {!uploadProgress && <DownOutlined style={{ marginLeft: 4, fontSize: 10 }} />}
                        </Button>
                      </Dropdown>
                    </>
                  ) : (
                    <Button type="primary" icon={<PictureOutlined />} onClick={() => imageInputRef.current?.click()}>
                      {t('上传图片')}
                    </Button>
                  )}
                  <Select
                    popupClassName="jx-mySpace-sourceFilterPopup"
                    className="jx-mySpace-sourceFilter"
                    value={sourceFilter}
                    onChange={setSourceFilter}
                    options={[
                      { value: 'all', label: t('全部来源') },
                      { value: 'user_upload', label: t('用户上传') },
                      { value: 'ai_generated', label: t('AI生成') },
                    ]}
                  />
                  <Input
                    className="jx-mySpace-search"
                    placeholder={t('搜索')}
                    prefix={<SearchOutlined style={{ color: 'var(--color-text-placeholder)' }} />}
                    value={searchKeyword}
                    onChange={(event) => handleSearch(event.target.value)}
                    allowClear
                  />
                </div>
              </div>
              <UploadProgressBar progress={uploadProgress} />
              <input ref={fileInputRef} type="file" multiple hidden onChange={(event) => {
                void handleFilesPicked(event.target.files);
                event.target.value = '';
              }} />
              <input
                ref={folderInputRef}
                type="file"
                // @ts-expect-error Chromium directory picker attribute.
                webkitdirectory=""
                directory=""
                multiple
                hidden
                onChange={(event) => {
                  void handleFolderPicked(event.target.files);
                  event.target.value = '';
                }}
              />
              <input ref={imageInputRef} type="file" accept="image/*" multiple hidden onChange={(event) => {
                void handleFilesPicked(event.target.files);
                event.target.value = '';
              }} />
            </>
          )}
        </div>

        <div className="jx-mySpace-body" onScroll={(event) => {
          if (tab === 'shares' || tab === 'notifications') return;
          const element = event.currentTarget;
          if (element.scrollHeight - element.scrollTop - element.clientHeight < 100) void loadMore();
        }}>
          <motion.div key={tab} className="jx-mySpace-bodyFade" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
            {tab === 'notifications' ? (
              <NotificationList />
            ) : tab === 'shares' ? (
              <ShareRecordsPage embedded hideEmbeddedDesc />
            ) : tab === 'assets' ? (
              <AnimatePresence mode="popLayout" custom={navDirection.current} initial={false}>
                <motion.div
                  key={`${selectedScope.folderId ?? 'root'}:${assetFilter}`}
                  className="jx-mySpace-scopeSlide"
                  custom={navDirection.current}
                  variants={scopeSlideVariants}
                  initial="enter"
                  animate="center"
                  exit="exit"
                >
                  {showSkeleton ? (
                    <MySpaceSkeleton tab={tab} assetFilter={assetFilter} />
                  ) : resources.length === 0 && (assetFilter !== 'document' || personalChildFolders.length === 0) ? (
                    renderEmpty(t('当前文件夹暂无内容，先新建文件夹或上传文件试试'))
                  ) : assetFilter === 'document' ? (
                    <DocumentList
                      items={resources}
                      onDownload={handleDownload}
                      onNavigate={handleNavigate}
                      onDelete={(item) => { void deleteResource(item.id); }}
                      onPreview={handlePreview}
                      onAddToKb={openKbPicker}
                      folders={personalChildFolders}
                      onEnterFolder={(folderId) => {
                        navDirection.current = 1;
                        void enterPersonalFolder(folderId);
                      }}
                      onRenameFolder={handleRenameFolder}
                      onDeleteFolder={handleDeleteFolder}
                      onMoveToPersonalFolder={(item) => openMoveModal([item.id], 'move')}
                      onBulkMoveToPersonalFolder={(items) => openMoveModal(items.map((item) => item.id), 'move')}
                      onCopyToPersonalFolder={(item) => openMoveModal([item.id], 'copy')}
                      onBulkCopyToPersonalFolder={(items) => openMoveModal(items.map((item) => item.id), 'copy')}
                    />
                  ) : (
                    <ImageGrid
                      items={resources}
                      onDownload={handleDownload}
                      onNavigate={handleNavigate}
                      onDelete={(item) => { void deleteResource(item.id); }}
                      onMoveToPersonalFolder={(item) => openMoveModal([item.id], 'move')}
                      onCopyToPersonalFolder={(item) => openMoveModal([item.id], 'copy')}
                    />
                  )}
                  {!loading && !hasMore && resources.length > 0 && (
                    <div className="jx-mySpace-noMore">{t('已加载全部内容')}</div>
                  )}
                </motion.div>
              </AnimatePresence>
            ) : loading && currentItems.length === 0 ? (
              <MySpaceSkeleton tab={tab} assetFilter={assetFilter} />
            ) : currentItems.length === 0 ? (
              renderEmpty(t('暂无内容'))
            ) : (
              <>
                <FavoriteList
                  items={favorites}
                  onNavigate={handleNavigate}
                  onRequestUnfavorite={handleRequestUnfavorite}
                  onFinalizeUnfavorite={(item) => {
                    if (item.source_chat_id) removeFavorite(item.source_chat_id);
                  }}
                />
                {!loading && !currentHasMore && (
                  <div className="jx-mySpace-noMore">{t('已加载全部内容')}</div>
                )}
              </>
            )}
          </motion.div>
        </div>
      </div>

      <DropOverlay active={dragActive && canDropUpload} hint={t('松开，上传到当前文件夹')} />
      <CreatePersonalFolderModal open={createFolderOpen} onClose={() => setCreateFolderOpen(false)} />
      <MoveToPersonalFolderModal
        open={moveFolderOpen}
        mode={moveMode}
        artifactIds={moveArtifactIds}
        onClose={() => {
          setMoveFolderOpen(false);
          setMoveArtifactIds([]);
        }}
      />
      <Modal
        title={t('加入私有知识库')}
        open={kbPickerOpen}
        onCancel={() => setKbPickerOpen(false)}
        footer={[
          <Button key="cancel" onClick={() => setKbPickerOpen(false)}>{t('取消')}</Button>,
          <Button key="submit" type="primary" loading={kbPickerLoading} onClick={() => void handleAddToKb()}>
            {t('确认加入')}
          </Button>,
        ]}
      >
        <Select
          className="jx-mySpace-kbPickerSelect"
          mode="multiple"
          value={selectedKbIds}
          onChange={setSelectedKbIds}
          placeholder={t('请选择一个或多个私有知识库')}
          options={privateKbOptions.map((item) => ({ value: item.id, label: item.name }))}
        />
      </Modal>
    </div>
  );
}
