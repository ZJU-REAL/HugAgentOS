import { usePersonalAgentName } from '../../personalAgentEdition';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { FolderAddOutlined } from '@ant-design/icons';
import { message, Modal } from 'antd';

import { addArtifactToKnowledgeBase, getApiUrl, getAutomationRuns } from '../../api';
import { useDelayedFlag } from '../../hooks';
import { useFileDropZone } from '../../hooks/useFileDropZone';
import { t } from '../../i18n';
import { useAutomationChatStore, useCanvasStore, useCatalogStore, useChatStore } from '../../stores';
import { useMySpaceTab } from '../../routing/subPages';
import { useMySpaceStore } from '../../stores/mySpaceStore';
import type { KBItem, MySpaceTab, ResourceItem } from '../../types';
import { buildFileUrl } from '../../utils/constants';
import { findFolderById } from '../../utils/folderTree';

import { useAssetUploadStore, startAssetUpload, restoreAssetUpload } from '../../stores/assetUploadStore';
import { pickedFiles, readDroppedFiles, type UploadSelection } from '../../utils/folderUpload';
import { useAuthStore } from '../../stores/authStore';

const TABS: Array<{ key: MySpaceTab; label: string }> = [
  { key: 'assets', label: t('文件资产') },
  // 知识库原本是侧边栏一级入口，现在收进我的空间，紧挨「文件资产」——文件资产里的文档本来就是
  // 「可按需加入私有知识库」的上游，两者放一起更顺。面板复用 CatalogPanel（embedded 模式去掉页头）。
  { key: 'kb', label: t('知识库') },
  { key: 'favorites', label: t('会话收藏') },
  { key: 'shares', label: t('分享记录') },
  { key: 'notifications', label: t('消息通知') },
];

export function useCeMySpaceController() {
  const agentName = usePersonalAgentName();
  const visibleTabs = agentName ? [...TABS, { key: 'agent' as const, label: agentName }] : TABS;
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
  } = useMySpaceStore();
  const { catalog, setPanel } = useCatalogStore();
  // 模块由地址决定：直接打开 /my-space/<模块> 或按前进后退时把数据切过去
  const routeTab = useMySpaceTab();
  useEffect(() => {
    if (useMySpaceStore.getState().tab !== routeTab) useMySpaceStore.getState().syncTab(routeTab);
  }, [routeTab]);
  const setCurrentChatId = useChatStore((state) => state.setCurrentChatId);
  const openCanvas = useCanvasStore((state) => state.openCanvas);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const navDirection = useRef(0);
  const { task: uploadTask, busy: uploadBusy } = useAssetUploadStore();
  const authUser = useAuthStore(state => state.authUser);
  useEffect(() => { restoreAssetUpload(); }, [authUser, uploadBusy]);
  const ownUpload = uploadTask?.owner === String(authUser?.user_id ?? '') && uploadTask?.api === getApiUrl();
  const uploadProgress = uploadBusy && ownUpload
    ? { done: uploadTask?.items.filter(item => item.status === 'done').length ?? 0, total: uploadTask?.items.length ?? 0 }
    : null;
  useEffect(() => {
    if (!ownUpload || uploadBusy || !uploadTask || !['finished', 'failed'].includes(uploadTask.phase)) return;
    void loadPersonalFolderTree();
    void fetchResources(true);
  }, [ownUpload, uploadBusy, uploadTask?.id, uploadTask?.phase, loadPersonalFolderTree, fetchResources]);
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

  const beginUpload = useCallback(async (selection: Promise<UploadSelection>) => {
    try {
      await startAssetUpload(selection, { kind: 'personal', folderId: selectedScope.folderId ?? null });
    } catch (error) {
      message.error(error instanceof Error ? error.message : t('上传失败'));
    }
  }, [selectedScope.folderId]);

  const handleFilesPicked = useCallback((files: FileList | null) => {
    if (files?.length) void beginUpload(Promise.resolve(pickedFiles(files)));
  }, [beginUpload]);
  const handleFolderPicked = handleFilesPicked;

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
        .catch((error) => message.error((error as Error)?.message || t('打开定时任务失败')));
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

  const canDropUpload = !uploadBusy && tab === 'assets';
  const { dragActive, dropZoneProps } = useFileDropZone(canDropUpload, handleFilesPicked, (data) => {
    void beginUpload(readDroppedFiles(data));
  });
  const showSkeleton = useDelayedFlag(loading && resources.length === 0);
  const currentItems = tab === 'favorites' ? favorites : resources;
  const currentHasMore = tab === 'favorites' ? favHasMore : hasMore;
  const tabDescriptions: Record<MySpaceTab, string> = {
    assets: t('汇集与AI会话过程中上传或生成的各类文档与图片，可按需加入你创建的私有知识库'),
    kb: t('浏览与管理知识库、查看文档列表，并支持文档内检索'),
    favorites: t('集中管理你收藏的重要会话与定时任务，方便快速回看与继续交流'),
    shares: t('查看并管理已生成的分享链接与有效状态，查看浏览量'),
    agent: agentName,
    notifications: t('查看定时任务执行结果通知，及时了解任务完成状态'),
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

  return { visibleTabs, resources, favorites, loading, tab, searchKeyword, hasMore, assetFilter, sourceFilter, notifUnreadCount, selectedScope, personalChildFolders, setTab, setAssetFilter, setSourceFilter, deleteResource, removeFavorite, loadMore, enterPersonalFolder, fileInputRef, folderInputRef, imageInputRef, navDirection, uploadProgress, createFolderOpen, setCreateFolderOpen, moveFolderOpen, setMoveFolderOpen, moveArtifactIds, setMoveArtifactIds, moveMode, kbPickerOpen, setKbPickerOpen, kbPickerLoading, selectedKbIds, setSelectedKbIds, parentFolderId, handleSearch, handleFilesPicked, handleFolderPicked, handleRenameFolder, handleDeleteFolder, openMoveModal, handleDownload, handleNavigate, handlePreview, handleRequestUnfavorite, privateKbOptions, openKbPicker, handleAddToKb, canDropUpload, dragActive, dropZoneProps, showSkeleton, currentItems, currentHasMore, tabDescriptions, renderEmpty };
}
