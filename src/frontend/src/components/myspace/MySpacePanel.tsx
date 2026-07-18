import { useEffect, useCallback, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import type { Variants } from 'motion/react';
import { Button, Dropdown, Input, Modal, Select, message } from 'antd';
import { ArrowLeftOutlined, DownOutlined, FileOutlined, FolderAddOutlined, FolderOpenOutlined, PictureOutlined, SafetyOutlined, SearchOutlined, UploadOutlined } from '@ant-design/icons';
import { addArtifactToKnowledgeBase, createPersonalFolder, createTeamFolder, getAutomationRuns, uploadFile, uploadTeamFile } from '../../api';
import type { KBItem, MySpaceTab, ResourceItem } from '../../types';
import type { TeamFolderNode } from '../../types/teamFiles';
import { scopeCacheKey } from '../../types/teamFiles';
import { EASE, SPRING } from '../../utils/motionTokens';
import { useMySpaceStore } from '../../stores/mySpaceStore';
import { useAutomationChatStore } from '../../stores/automationChatStore';
import { useCatalogStore, useChatStore, useCanvasStore, useEditionStore } from '../../stores';
import { buildFileUrl } from '../../utils/constants';
import { findFolderById } from '../../utils/folderTree';
import { resolvedAtLeast } from '../../utils/roles';
import { DocumentList } from './DocumentList';
import { ImageGrid } from './ImageGrid';
import { FavoriteList } from './FavoriteList';
import { NotificationList } from './NotificationList';
import { MySpaceSkeleton } from './MySpaceSkeleton';
import { ShareRecordsPage } from '../share';
import { TeamScopeTree } from './team/TeamScopeTree';
import { TeamFolderBreadcrumb } from './team/TeamFolderBreadcrumb';
import { MoveToTeamModal } from './team/MoveToTeamModal';
import { TeamPermissionsModal } from './team/TeamPermissionsModal';
import {
  CreatePersonalFolderModal,
  MoveToPersonalFolderModal,
} from './personal';
import { DropOverlay } from '../common/DropOverlay';
import { UploadProgressBar } from '../common/UploadProgressBar';
import type { UploadProgress } from '../common/UploadProgressBar';
import { useDelayedFlag } from '../../hooks';
import { useFileDropZone } from '../../hooks/useFileDropZone';
import { t } from '../../i18n';

const TABS: Array<{ key: MySpaceTab; label: string }> = [
  { key: 'assets', label: t('文件资产') },
  { key: 'favorites', label: t('会话收藏') },
  { key: 'shares', label: t('分享记录') },
  { key: 'notifications', label: t('消息通知') },
];

const UPLOAD_CONCURRENCY = 4;

/** Folder navigation direction: enterFolder=1 (enter from right), go up=-1 (enter from left), same-level switch=0 (pure fade) */
const scopeSlideVariants: Variants = {
  enter: (dir: number) => ({ opacity: 0, x: dir * 24 }),
  center: { opacity: 1, x: 0, transition: { duration: 0.22, ease: EASE.brandOut } },
  exit: (dir: number) => ({ opacity: 0, x: dir * -24, transition: { duration: 0.16, ease: EASE.exit } }),
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
  const enterAutomationChat = useAutomationChatStore((s) => s.enterAutomationChat);
  const exitAutomationChat = useAutomationChatStore((s) => s.exitAutomationChat);
  const {
    resources, favorites, loading, tab, searchKeyword, hasMore, favHasMore,
    assetFilter, sourceFilter, notifUnreadCount,
    setTab, setSearchKeyword, setAssetFilter, setSourceFilter,
    fetchResources, fetchFavorites, deleteResource, unfavoriteChat, removeFavorite, loadMore,
    assetScope, setAssetScope,
    selectedScope, myTeams, loadTeamFolderTree,
    uploadToCurrentScope,
    loadMyTeams,
    personalChildFolders,
    personalFolderTree,
    loadPersonalFolderTree,
    enterPersonalFolder,
    renamePersonalFolderAction,
    deletePersonalFolderAction,
    uploadPersonalFile,
  } = useMySpaceStore();
  const personalParentFolderId = useMemo<string | null>(() => {
    if (selectedScope.kind !== 'personal' || !selectedScope.folderId) return null;
    return findFolderById(personalFolderTree, selectedScope.folderId)?.parent_folder_id ?? null;
  }, [selectedScope, personalFolderTree]);
  const { catalog, setPanel } = useCatalogStore();
  const { setCurrentChatId } = useChatStore();
  const openCanvas = useCanvasStore((s) => s.openCanvas);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [kbPickerOpen, setKbPickerOpen] = useState(false);
  const [kbPickerLoading, setKbPickerLoading] = useState(false);
  const [selectedKbIds, setSelectedKbIds] = useState<string[]>([]);
  const [pendingResources, setPendingResources] = useState<ResourceItem[]>([]);
  const [moveModalOpen, setMoveModalOpen] = useState(false);
  const [moveArtifactIds, setMoveArtifactIds] = useState<string[]>([]);
  const [copyModalOpen, setCopyModalOpen] = useState(false);
  const [copyArtifactIds, setCopyArtifactIds] = useState<string[]>([]);
  const [copyFolderId, setCopyFolderId] = useState<string | undefined>(undefined);
  const [permsModalTeamId, setPermsModalTeamId] = useState<string | null>(null);
  const [createPersonalFolderOpen, setCreatePersonalFolderOpen] = useState(false);
  const [movePersonalModalOpen, setMovePersonalModalOpen] = useState(false);
  const [movePersonalArtifactIds, setMovePersonalArtifactIds] = useState<string[]>([]);
  const [movePersonalMode, setMovePersonalMode] = useState<'move' | 'copy'>('move');
  const personalFileInputRef = useRef<HTMLInputElement | null>(null);
  const personalFolderInputRef = useRef<HTMLInputElement | null>(null);
  const personalImageInputRef = useRef<HTMLInputElement | null>(null);
  const [personalFolderUploading, setPersonalFolderUploading] = useState<UploadProgress | null>(null);
  const teamFileInputRef = useRef<HTMLInputElement | null>(null);
  const teamFolderInputRef = useRef<HTMLInputElement | null>(null);
  const [teamFolderUploading, setTeamFolderUploading] = useState<UploadProgress | null>(null);
  /** Folder navigation direction: 1=enter subfolder (from right), -1=go up (from left), 0=same-level switch (pure fade) */
  const navDirRef = useRef(0);

  const openMoveToTeam = useCallback((ids: string[]) => {
    setMoveArtifactIds(ids);
    setMoveModalOpen(true);
  }, []);

  const openCopyToTeam = useCallback((ids: string[]) => {
    setCopyFolderId(undefined);
    setCopyArtifactIds(ids);
    setCopyModalOpen(true);
  }, []);

  const openCopyFolderToTeam = useCallback((folderId: string) => {
    setCopyArtifactIds([]);
    setCopyFolderId(folderId);
    setCopyModalOpen(true);
  }, []);

  const openMoveToPersonalFolder = useCallback((ids: string[]) => {
    setMovePersonalArtifactIds(ids);
    setMovePersonalMode('move');
    setMovePersonalModalOpen(true);
  }, []);

  const openCopyToPersonalFolder = useCallback((ids: string[]) => {
    setMovePersonalArtifactIds(ids);
    setMovePersonalMode('copy');
    setMovePersonalModalOpen(true);
  }, []);

  // Resolved permissions for the current team
  const currentTeam = useMemo(() => (
    selectedScope.kind === 'team'
      ? myTeams.find((t) => t.team_id === selectedScope.teamId)
      : undefined
  ), [selectedScope, myTeams]);
  const resolvedPerm = currentTeam?.resolved ?? 'view';
  const isTeamScope = selectedScope.kind === 'team';
  const canEditCurrent = isTeamScope && resolvedAtLeast(resolvedPerm, 'edit');
  const canAdminCurrent = isTeamScope && resolvedAtLeast(resolvedPerm, 'admin');

  // Team folders are a multi-tenant capability bit (under CE / unlicensed, hide the tab and fall back to personal)
  const multiTenancy = useEditionStore((s) => (s.loaded ? !!s.features.multi_tenancy : true));

  useEffect(() => { void loadMyTeams(); }, [loadMyTeams]);
  useEffect(() => {
    if (assetScope === 'personal') void loadPersonalFolderTree();
  }, [assetScope, loadPersonalFolderTree]);
  useEffect(() => {
    if (!multiTenancy && assetScope === 'team') setAssetScope('personal');
  }, [multiTenancy, assetScope, setAssetScope]);

  const handleEnterFolder = useCallback((folderId: string) => {
    navDirRef.current = 1;
    void enterPersonalFolder(folderId);
  }, [enterPersonalFolder]);

  const handleRenameFolder = useCallback(async (folderId: string, currentName: string) => {
    let next = currentName;
    Modal.confirm({
      title: t('重命名文件夹'),
      icon: <FolderAddOutlined />,
      content: (
        <input
          className="jx-team-folder-input"
          autoFocus
          defaultValue={currentName}
          maxLength={255}
          onChange={(e) => { next = e.target.value; }}
        />
      ),
      okText: t('保存'),
      cancelText: t('取消'),
      onOk: async () => {
        const trimmed = next.trim();
        if (!trimmed) {
          message.warning(t('名称不能为空'));
          throw new Error('empty');
        }
        try {
          await renamePersonalFolderAction(folderId, trimmed);
          message.success(t('已重命名'));
        } catch (e: any) {
          message.error(e?.message || t('重命名失败'));
          throw e;
        }
      },
    });
  }, [renamePersonalFolderAction]);

  const handleDeletePersonalFolder = useCallback((folderId: string, name: string) => {
    Modal.confirm({
      title: t('删除文件夹"{name}"？', { name }),
      content: t('该文件夹及其所有子文件夹、文件都将被软删除（可在数据库中找回）。'),
      okText: t('删除'),
      okType: 'danger',
      cancelText: t('取消'),
      onOk: async () => {
        try {
          const affected = await deletePersonalFolderAction(folderId);
          message.success(affected > 0 ? t('已删除文件夹及其下 {affected} 个文件', { affected }) : t('文件夹已删除'));
        } catch (e: any) {
          message.error(e?.message || t('删除失败'));
        }
      },
    });
  }, [deletePersonalFolderAction]);

  const handlePersonalFilesPicked = useCallback(async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const arr = Array.from(files);
    setPersonalFolderUploading({ done: 0, total: arr.length });
    let done = 0;
    let failed = 0;
    await runWithConcurrency(arr, UPLOAD_CONCURRENCY, async (f) => {
      try {
        await uploadPersonalFile(f);
      } catch (e: any) {
        failed += 1;
        message.error(`${f.name}: ${e?.message || t('上传失败')}`);
      }
      done += 1;
      setPersonalFolderUploading({ done, total: arr.length });
    });
    setPersonalFolderUploading(null);
    if (arr.length - failed > 0) {
      message.success(arr.length > 1 ? t('已上传 {n} 个文件', { n: arr.length - failed }) : t('已上传'));
    }
  }, [uploadPersonalFile]);

  const handlePersonalFolderPicked = useCallback(async (files: FileList | null) => {
    const arr = Array.from(files || []);
    if (arr.length === 0) return;

    const baseFolderId = selectedScope.kind === 'personal' ? (selectedScope.folderId ?? null) : null;

    const dirsByDepth = new Map<number, Set<string>>();
    const fileEntries: Array<{ file: File; relDir: string }> = [];
    for (const f of arr) {
      const rel = (f as any).webkitRelativePath || '';
      const lastSlash = rel.lastIndexOf('/');
      const dirPath = lastSlash === -1 ? '' : rel.slice(0, lastSlash);
      if (dirPath) {
        const segs = dirPath.split('/').filter(Boolean);
        let acc = '';
        for (let i = 0; i < segs.length; i += 1) {
          acc = acc ? `${acc}/${segs[i]}` : segs[i];
          if (!dirsByDepth.has(i + 1)) dirsByDepth.set(i + 1, new Set());
          dirsByDepth.get(i + 1)!.add(acc);
        }
      }
      fileEntries.push({ file: f, relDir: dirPath });
    }

    setPersonalFolderUploading({ done: 0, total: fileEntries.length });

    const pathToId = new Map<string, string | null>();
    pathToId.set('', baseFolderId);
    // On name collision, refetch the tree once on demand and cache it for reuse on subsequent collisions
    let cachedTree: typeof personalFolderTree | null = null;
    const ensureTree = async () => {
      if (!cachedTree) {
        await loadPersonalFolderTree();
        cachedTree = useMySpaceStore.getState().personalFolderTree;
      }
      return cachedTree;
    };
    const findChildByName = (
      tree: typeof personalFolderTree,
      parentId: string | null,
      name: string,
    ): string | null => {
      if (parentId === null) {
        const hit = tree.find((n) => n.name === name);
        return hit?.folder_id ?? null;
      }
      const parent = findFolderById(tree, parentId);
      return parent?.children?.find((c) => c.name === name)?.folder_id ?? null;
    };

    const depths = Array.from(dirsByDepth.keys()).sort((a, b) => a - b);
    for (const depth of depths) {
      const paths = Array.from(dirsByDepth.get(depth)!);
      const results = await Promise.allSettled(paths.map(async (dirPath) => {
        const lastSlash = dirPath.lastIndexOf('/');
        const parentPath = lastSlash === -1 ? '' : dirPath.slice(0, lastSlash);
        const folderName = lastSlash === -1 ? dirPath : dirPath.slice(lastSlash + 1);
        const parentId = pathToId.get(parentPath) ?? null;
        try {
          const res = await createPersonalFolder(folderName, parentId);
          return { dirPath, folderId: res.folder_id };
        } catch (e: any) {
          const tree = await ensureTree();
          const found = findChildByName(tree, parentId, folderName);
          if (found) return { dirPath, folderId: found };
          throw new Error(e?.message || dirPath);
        }
      }));
      for (const r of results) {
        if (r.status === 'fulfilled') {
          pathToId.set(r.value.dirPath, r.value.folderId);
        } else {
          message.error(`${t('建文件夹失败：')}${(r.reason as Error).message}`);
          setPersonalFolderUploading(null);
          return;
        }
      }
    }

    let done = 0;
    let failed = 0;
    await runWithConcurrency(fileEntries, UPLOAD_CONCURRENCY, async (entry) => {
      const targetId = pathToId.get(entry.relDir) ?? null;
      try {
        await uploadFile(entry.file, undefined, targetId);
      } catch {
        failed += 1;
      }
      done += 1;
      setPersonalFolderUploading({ done, total: fileEntries.length });
    });

    setPersonalFolderUploading(null);
    if (failed > 0) {
      message.warning(t('上传完成：成功 {ok} 个，失败 {failed} 个', { ok: fileEntries.length - failed, failed }));
    } else {
      message.success(t('已上传 {n} 个文件', { n: fileEntries.length }));
    }
    await loadPersonalFolderTree();
    await fetchResources(true);
  }, [selectedScope, loadPersonalFolderTree, fetchResources]);

  const handleTeamFilesPicked = useCallback(async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    if (selectedScope.kind !== 'team' || !selectedScope.teamId) return;
    const arr = Array.from(files);
    setTeamFolderUploading({ done: 0, total: arr.length });
    let done = 0;
    let failed = 0;
    await runWithConcurrency(arr, UPLOAD_CONCURRENCY, async (f) => {
      try {
        await uploadToCurrentScope(f);
      } catch (e: any) {
        failed += 1;
        message.error(`${f.name}: ${e?.message || t('上传失败')}`);
      }
      done += 1;
      setTeamFolderUploading({ done, total: arr.length });
    });
    setTeamFolderUploading(null);
    if (arr.length - failed > 0) {
      message.success(
        arr.length > 1
          ? t('已上传 {n} 个文件到团队文件夹', { n: arr.length - failed })
          : t('已上传到团队文件夹'),
      );
    }
  }, [selectedScope, uploadToCurrentScope]);

  const handleTeamFolderPicked = useCallback(async (files: FileList | null) => {
    const arr = Array.from(files || []);
    if (arr.length === 0) return;
    if (selectedScope.kind !== 'team' || !selectedScope.teamId) return;
    const teamId = selectedScope.teamId;
    const baseFolderId = selectedScope.folderId ?? null;

    const dirsByDepth = new Map<number, Set<string>>();
    const fileEntries: Array<{ file: File; relDir: string }> = [];
    for (const f of arr) {
      const rel = (f as any).webkitRelativePath || '';
      const lastSlash = rel.lastIndexOf('/');
      const dirPath = lastSlash === -1 ? '' : rel.slice(0, lastSlash);
      if (dirPath) {
        const segs = dirPath.split('/').filter(Boolean);
        let acc = '';
        for (let i = 0; i < segs.length; i += 1) {
          acc = acc ? `${acc}/${segs[i]}` : segs[i];
          if (!dirsByDepth.has(i + 1)) dirsByDepth.set(i + 1, new Set());
          dirsByDepth.get(i + 1)!.add(acc);
        }
      }
      fileEntries.push({ file: f, relDir: dirPath });
    }

    setTeamFolderUploading({ done: 0, total: fileEntries.length });

    const pathToId = new Map<string, string | null>();
    pathToId.set('', baseFolderId);
    // On name collision, refetch the tree once on demand and cache it for reuse on subsequent collisions
    let cachedTree: TeamFolderNode[] | null = null;
    const ensureTree = async () => {
      if (!cachedTree) {
        await loadTeamFolderTree(teamId);
        cachedTree = useMySpaceStore.getState().folderTreesByTeam[teamId] ?? [];
      }
      return cachedTree;
    };
    const findChildByName = (
      tree: TeamFolderNode[],
      parentId: string | null,
      name: string,
    ): string | null => {
      if (parentId === null) {
        const hit = tree.find((n) => n.name === name);
        return hit?.folder_id ?? null;
      }
      const parent = findFolderById(tree, parentId);
      return parent?.children?.find((c) => c.name === name)?.folder_id ?? null;
    };

    const depths = Array.from(dirsByDepth.keys()).sort((a, b) => a - b);
    for (const depth of depths) {
      const paths = Array.from(dirsByDepth.get(depth)!);
      const results = await Promise.allSettled(paths.map(async (dirPath) => {
        const lastSlash = dirPath.lastIndexOf('/');
        const parentPath = lastSlash === -1 ? '' : dirPath.slice(0, lastSlash);
        const folderName = lastSlash === -1 ? dirPath : dirPath.slice(lastSlash + 1);
        const parentId = pathToId.get(parentPath) ?? null;
        try {
          const res = await createTeamFolder(teamId, folderName, parentId);
          return { dirPath, folderId: res.folder_id };
        } catch (e: any) {
          const tree = await ensureTree();
          const found = findChildByName(tree, parentId, folderName);
          if (found) return { dirPath, folderId: found };
          throw new Error(e?.message || dirPath);
        }
      }));
      for (const r of results) {
        if (r.status === 'fulfilled') {
          pathToId.set(r.value.dirPath, r.value.folderId);
        } else {
          message.error(`${t('建文件夹失败：')}${(r.reason as Error).message}`);
          setTeamFolderUploading(null);
          return;
        }
      }
    }

    let done = 0;
    let failed = 0;
    await runWithConcurrency(fileEntries, UPLOAD_CONCURRENCY, async (entry) => {
      const targetId = pathToId.get(entry.relDir) ?? null;
      try {
        await uploadTeamFile(teamId, targetId, entry.file);
      } catch {
        failed += 1;
      }
      done += 1;
      setTeamFolderUploading({ done, total: fileEntries.length });
    });

    setTeamFolderUploading(null);
    if (failed > 0) {
      message.warning(t('上传完成：成功 {ok} 个，失败 {failed} 个', { ok: fileEntries.length - failed, failed }));
    } else {
      message.success(t('已上传 {n} 个文件到团队文件夹', { n: fileEntries.length }));
    }
    await loadTeamFolderTree(teamId);
    await fetchResources(true);
  }, [selectedScope, loadTeamFolderTree, fetchResources]);

  const handleCreateFolder = useCallback(() => {
    if (selectedScope.kind !== 'team' || !selectedScope.teamId) return;
    const teamId = selectedScope.teamId;
    const parentFolderId = selectedScope.folderId;
    let name = '';
    Modal.confirm({
      title: parentFolderId ? t('新建子文件夹') : t('新建团队文件夹'),
      icon: <FolderAddOutlined />,
      content: (
        <input
          className="jx-team-folder-input"
          autoFocus
          placeholder={t('输入文件夹名称')}
          maxLength={60}
          onChange={(e) => { name = e.target.value; }}
        />
      ),
      okText: t('创建'),
      cancelText: t('取消'),
      onOk: async () => {
        const trimmed = name.trim();
        if (!trimmed) {
          message.warning(t('请输入文件夹名称'));
          throw new Error('empty');
        }
        try {
          await createTeamFolder(teamId, trimmed, parentFolderId);
          await loadTeamFolderTree(teamId);
          message.success(t('已创建'));
        } catch (e: any) {
          message.error(e?.message || t('创建失败'));
          throw e;
        }
      },
    });
  }, [selectedScope, loadTeamFolderTree]);

  // Initial fetch on mount
  useEffect(() => {
    if (tab === 'favorites') {
      void fetchFavorites(true);
    } else if (tab === 'assets') {
      void fetchResources(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => () => {
    if (searchTimer.current) {
      clearTimeout(searchTimer.current);
    }
  }, []);

  // Search debounce
  const handleSearch = useCallback((value: string) => {
    setSearchKeyword(value);
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => {
      if (tab === 'favorites') {
        void fetchFavorites(true);
      } else if (tab === 'assets') {
        void fetchResources(true);
      }
    }, 300);
  }, [tab, setSearchKeyword, fetchFavorites, fetchResources]);

  const handleDownload = useCallback((item: ResourceItem) => {
    if (!item.file_id) return;
    const a = document.createElement('a');
    a.href = buildFileUrl(item.file_id);
    a.download = item.name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }, []);

  const handleNavigate = useCallback((item: ResourceItem) => {
    if (!item.source_chat_id) return;
    if (item.source_chat_id.startsWith('automation:')) {
      const taskId = item.source_chat_id.slice('automation:'.length);
      void (async () => {
        try {
          const runs = await getAutomationRuns(taskId, 50);
          enterAutomationChat(taskId, item.source_chat_title || item.name || t('自动化任务'), runs);
        } catch (err: any) {
          message.error(err?.message || t('打开自动化任务失败'));
        }
      })();
      return;
    }
    if (useAutomationChatStore.getState().activeGroup) exitAutomationChat();
    setCurrentChatId(item.source_chat_id);
    setPanel('chat');
  }, [enterAutomationChat, exitAutomationChat, setCurrentChatId, setPanel]);

  const handleDelete = useCallback((item: ResourceItem) => {
    void deleteResource(item.id);
  }, [deleteResource]);

  const handleRequestUnfavorite = useCallback(async (item: ResourceItem) => {
    if (!item.source_chat_id) return;
    return await new Promise<boolean>((resolve) => {
      Modal.confirm({
        title: t('取消收藏'),
        content: t('确定将这条会话从收藏列表中移除吗？'),
        okText: t('取消收藏'),
        cancelText: t('保留'),
        onOk: async () => {
          try {
            await unfavoriteChat(item.source_chat_id as string);
            message.success(t('已取消收藏'));
            resolve(true);
          } catch (err: any) {
            message.error(err?.message || t('取消收藏失败'));
            resolve(false);
          }
        },
        onCancel: () => resolve(false),
      });
    });
  }, [unfavoriteChat]);

  const handleFinalizeUnfavorite = useCallback((item: ResourceItem) => {
    if (!item.source_chat_id) return;
    removeFavorite(item.source_chat_id);
  }, [removeFavorite]);

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

  // Scroll-to-load-more
  const handleScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 100) {
      void loadMore();
    }
  }, [loadMore]);

  const currentItems = tab === 'favorites' ? favorites : resources;
  const currentHasMore = tab === 'favorites' ? favHasMore : hasMore;
  const teamSkeletonVisible = useDelayedFlag(loading && resources.length === 0);
  const currentSkeletonVisible = useDelayedFlag(loading && currentItems.length === 0);

  const privateKbOptions = useMemo<KBItem[]>(
    () => catalog.kb.filter((item) => item.visibility === 'private' && item.editable !== false && !item.system_managed),
    [catalog.kb],
  );

  const openKbPicker = useCallback((items: ResourceItem | ResourceItem[]) => {
    if (privateKbOptions.length === 0) {
      message.warning(t('请先创建至少一个私有知识库'));
      return;
    }
    const nextItems = (Array.isArray(items) ? items : [items]).filter((item) => !!item.file_id);
    if (nextItems.length === 0) {
      message.warning(t('当前选择中没有可加入知识库的文件'));
      return;
    }
    setPendingResources(nextItems);
    setSelectedKbIds(privateKbOptions[0]?.id ? [privateKbOptions[0].id] : []);
    setKbPickerOpen(true);
  }, [privateKbOptions]);

  const closeKbPicker = useCallback(() => {
    if (kbPickerLoading) return;
    setKbPickerOpen(false);
    setPendingResources([]);
    setSelectedKbIds([]);
  }, [kbPickerLoading]);

  const handleAddToKb = useCallback(async () => {
    if (pendingResources.length === 0 || selectedKbIds.length === 0) {
      message.warning(t('请至少选择一个目标知识库'));
      return;
    }
    setKbPickerLoading(true);
    try {
      const results = await Promise.all(
        pendingResources.flatMap((resource) => (
          selectedKbIds.map(async (kbId) => ({
            resourceId: resource.id,
            result: await addArtifactToKnowledgeBase(resource.id, kbId),
          }))
        ))
      );
      const alreadyExistsCount = results.filter(({ result }) => result.already_exists).length;
      const addedCount = results.length - alreadyExistsCount;
      const fileCount = pendingResources.length;
      const kbCount = selectedKbIds.length;
      if (addedCount > 0 && alreadyExistsCount > 0) {
        message.success(t('已处理 {fileCount} 个文件，{addedCount} 条加入成功，{alreadyExistsCount} 条已存在', { fileCount, addedCount, alreadyExistsCount }));
      } else if (addedCount > 0) {
        message.success(t('已将 {fileCount} 个文件加入 {kbCount} 个知识库，正在索引', { fileCount, kbCount }));
      } else {
        message.success(t('所选知识库中均已存在这些文件'));
      }
      await fetchResources(true);
      setKbPickerOpen(false);
      setPendingResources([]);
      setSelectedKbIds([]);
    } catch (err: any) {
      message.error(err?.message || t('加入知识库失败'));
    } finally {
      setKbPickerLoading(false);
    }
  }, [pendingResources, selectedKbIds, fetchResources]);

  // ── Drag-and-drop upload (file assets page; disabled for team scope per canEditCurrent) ──
  const canDropUpload = tab === 'assets' && (
    assetScope === 'personal'
      ? selectedScope.kind === 'personal'
      : (canEditCurrent && selectedScope.kind === 'team' && !!selectedScope.teamId)
  );

  const handleDropFiles = useCallback((files: FileList) => {
    if (selectedScope.kind === 'team') {
      void handleTeamFilesPicked(files);
    } else {
      void handlePersonalFilesPicked(files);
    }
  }, [selectedScope, handleTeamFilesPicked, handlePersonalFilesPicked]);

  const { dragActive, dropZoneProps } = useFileDropZone(canDropUpload, handleDropFiles);

  const tabDescriptions: Record<MySpaceTab, string> = {
    assets: t('汇集与AI会话过程中上传或生成的各类文档与图片，可按需加入你创建的私有知识库'),
    favorites: t('集中管理你收藏的重要会话与自动化任务，方便快速回看与继续交流'),
    shares: t('查看并管理已生成的分享链接与有效状态，查看浏览量'),
    notifications: t('查看自动化任务执行结果通知，及时了解任务完成状态'),
  };

  return (
    <div className="jx-mySpace" {...dropZoneProps}>
      <div className="jx-mySpace-shell">
        <div className="jx-mySpace-header">
          <div className="jx-mySpace-tabs">
            {TABS.map((t) => (
              <button
                key={t.key}
                className={`jx-mySpace-tab${tab === t.key ? ' active' : ''}`}
                onClick={() => { navDirRef.current = 0; setTab(t.key); }}
              >
                <span>{t.label}</span>
                {t.key === 'notifications' && notifUnreadCount > 0 && (
                  <motion.span
                    key={notifUnreadCount}
                    className="jx-mySpace-tabBadge"
                    initial={{ scale: 0.6, opacity: 0 }}
                    animate={{ scale: 1, opacity: 1 }}
                    transition={SPRING.pop}
                  >
                    {notifUnreadCount > 99 ? '99+' : notifUnreadCount}
                  </motion.span>
                )}
                {tab === t.key && (
                  <motion.span
                    layoutId="jx-mySpace-tabInk"
                    className="jx-mySpace-tabInk"
                    transition={SPRING.ink}
                  />
                )}
              </button>
            ))}
          </div>
          <div className={`jx-mySpace-subHeader${tab === 'shares' ? ' jx-mySpace-subHeader-shares' : ''}`}>
            <p className="jx-mySpace-desc">{tabDescriptions[tab]}</p>
          </div>

          {tab === 'assets' && (
            <>
              <div className="jx-mySpace-subTabs" role="tablist" aria-label={t('文件归属')}>
                {([
                  { key: 'personal', label: t('个人文件夹') },
                  { key: 'team', label: t('团队文件夹') },
                ] as const).filter((item) => multiTenancy || item.key !== 'team').map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    role="tab"
                    aria-selected={assetScope === item.key}
                    className={`jx-mySpace-subTab${assetScope === item.key ? ' active' : ''}`}
                    onClick={() => { navDirRef.current = 0; setAssetScope(item.key); }}
                  >
                    {item.label}
                    {assetScope === item.key && (
                      <motion.span
                        layoutId="jx-mySpace-subTabInk"
                        className="jx-mySpace-tabInk jx-mySpace-tabInk--sub"
                        transition={SPRING.ink}
                      />
                    )}
                  </button>
                ))}
              </div>

              <div className="jx-mySpace-assetBar">
                <div className="jx-mySpace-filterTabs" role="tablist" aria-label={t('类型筛选')}>
                  {[
                    { key: 'document', label: t('文档') },
                    { key: 'image', label: t('图片') },
                  ].map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      role="tab"
                      aria-selected={assetFilter === item.key}
                      className={`jx-mySpace-filterTab${assetFilter === item.key ? ' active' : ''}`}
                      onClick={() => { navDirRef.current = 0; setAssetFilter(item.key as 'document' | 'image'); }}
                    >
                      {item.label}
                      {assetFilter === item.key && (
                        <motion.span
                          layoutId="jx-mySpace-filterTabInk"
                          className="jx-mySpace-tabInk jx-mySpace-tabInk--filter"
                          transition={SPRING.ink}
                        />
                      )}
                    </button>
                  ))}
                </div>
                <div className="jx-mySpace-filterTools">
                  {assetScope === 'personal' && selectedScope.kind === 'personal' && selectedScope.folderId !== null && (
                    <Button
                      type="text"
                      icon={<ArrowLeftOutlined />}
                      onClick={() => {
                        navDirRef.current = -1;
                        void enterPersonalFolder(personalParentFolderId);
                      }}
                    >
                      {t('返回上级')}
                    </Button>
                  )}
                  {assetScope === 'personal' && assetFilter === 'document' && (
                    <>
                      <Button
                        icon={<FolderAddOutlined />}
                        onClick={() => setCreatePersonalFolderOpen(true)}
                      >
                        {t('新建文件夹')}
                      </Button>
                      <Dropdown
                        trigger={['click']}
                        menu={{
                          items: [
                            {
                              key: 'file',
                              icon: <FileOutlined />,
                              label: t('上传文件'),
                              onClick: () => personalFileInputRef.current?.click(),
                            },
                            {
                              key: 'folder',
                              icon: <FolderOpenOutlined />,
                              label: t('上传文件夹'),
                              onClick: () => personalFolderInputRef.current?.click(),
                            },
                          ],
                        }}
                      >
                        <Button
                          type="primary"
                          icon={<UploadOutlined />}
                          loading={!!personalFolderUploading}
                        >
                          {personalFolderUploading
                            ? t('上传中 {done}/{total}', { done: personalFolderUploading.done, total: personalFolderUploading.total })
                            : t('上传文件')}
                          {!personalFolderUploading && <DownOutlined style={{ marginLeft: 4, fontSize: 10 }} />}
                        </Button>
                      </Dropdown>
                    </>
                  )}
                  {assetScope === 'personal' && assetFilter === 'image' && (
                    <Button
                      type="primary"
                      icon={<PictureOutlined />}
                      onClick={() => personalImageInputRef.current?.click()}
                    >
                      {t('上传图片')}
                    </Button>
                  )}
                  {assetScope === 'personal' && (
                    <Select
                      popupClassName="jx-mySpace-sourceFilterPopup"
                      className="jx-mySpace-sourceFilter"
                      value={sourceFilter}
                      onChange={(value) => setSourceFilter(value as 'all' | 'user_upload' | 'ai_generated')}
                      options={[
                        { value: 'all', label: t('全部来源') },
                        { value: 'user_upload', label: t('用户上传') },
                        { value: 'ai_generated', label: t('AI生成') },
                      ]}
                    />
                  )}
                  <Input
                    className="jx-mySpace-search"
                    placeholder={t('搜索')}
                    prefix={<SearchOutlined style={{ color: 'var(--color-text-placeholder)' }} />}
                    value={searchKeyword}
                    onChange={(e) => handleSearch(e.target.value)}
                    allowClear
                  />
                </div>
              </div>
              {assetScope === 'personal' && (
                <UploadProgressBar progress={personalFolderUploading} />
              )}
              {/* Hidden file / folder / image pickers for personal space */}
              <input
                ref={personalFileInputRef}
                type="file"
                multiple
                style={{ display: 'none' }}
                onChange={(e) => {
                  void handlePersonalFilesPicked(e.target.files);
                  e.target.value = '';
                }}
              />
              <input
                ref={personalFolderInputRef}
                type="file"
                // @ts-expect-error — Chrome / Edge specific attribute
                webkitdirectory=""
                directory=""
                multiple
                style={{ display: 'none' }}
                onChange={(e) => {
                  void handlePersonalFolderPicked(e.target.files);
                  e.target.value = '';
                }}
              />
              <input
                ref={personalImageInputRef}
                type="file"
                accept="image/*"
                multiple
                style={{ display: 'none' }}
                onChange={(e) => {
                  void handlePersonalFilesPicked(e.target.files);
                  e.target.value = '';
                }}
              />
            </>
          )}
        </div>

        <div className="jx-mySpace-body" onScroll={tab === 'shares' || tab === 'notifications' ? undefined : handleScroll}>
          {/* Tab-axis cross-fade: key bound only to tab (pure opacity, no exit; the scroll container itself is untouched).
              The scope / filter axes are each handled by inner keyed layers, avoiding double remount + stacked fade-in */}
          <motion.div
            key={tab}
            className="jx-mySpace-bodyFade"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.15, ease: EASE.standard }}
          >
          {tab === 'notifications' ? (
            <NotificationList />
          ) : tab === 'shares' ? (
            <ShareRecordsPage embedded hideEmbeddedDesc />
          ) : tab === 'assets' && assetScope === 'team' ? (
            <div className="jx-team-workspace">
              <aside className="jx-team-workspace-sidebar">
                <TeamScopeTree onManagePermissions={(teamId) => setPermsModalTeamId(teamId)} />
              </aside>
              <section className="jx-team-workspace-main">
                <div className="jx-team-workspace-toolbar">
                  <TeamFolderBreadcrumb />
                  <div className="jx-team-workspace-actions">
                    {isTeamScope && canEditCurrent && selectedScope.kind === 'team' && selectedScope.teamId && (
                      <Button
                        icon={<FolderAddOutlined />}
                        onClick={handleCreateFolder}
                      >
                        {t('新建文件夹')}
                      </Button>
                    )}
                    {isTeamScope && canEditCurrent && (
                      <>
                        <Dropdown
                          trigger={['click']}
                          menu={{
                            items: [
                              {
                                key: 'file',
                                icon: <FileOutlined />,
                                label: t('上传文件'),
                                onClick: () => teamFileInputRef.current?.click(),
                              },
                              {
                                key: 'folder',
                                icon: <FolderOpenOutlined />,
                                label: t('上传文件夹'),
                                onClick: () => teamFolderInputRef.current?.click(),
                              },
                            ],
                          }}
                        >
                          <Button
                            type="primary"
                            icon={<UploadOutlined />}
                            loading={!!teamFolderUploading}
                          >
                            {teamFolderUploading
                              ? t('上传中 {done}/{total}', { done: teamFolderUploading.done, total: teamFolderUploading.total })
                              : t('上传到团队')}
                            {!teamFolderUploading && <DownOutlined style={{ marginLeft: 4, fontSize: 10 }} />}
                          </Button>
                        </Dropdown>
                        <input
                          ref={teamFileInputRef}
                          type="file"
                          multiple
                          style={{ display: 'none' }}
                          onChange={(e) => {
                            void handleTeamFilesPicked(e.target.files);
                            e.target.value = '';
                          }}
                        />
                        <input
                          ref={teamFolderInputRef}
                          type="file"
                          // @ts-expect-error — Chrome / Edge specific attribute
                          webkitdirectory=""
                          directory=""
                          multiple
                          style={{ display: 'none' }}
                          onChange={(e) => {
                            void handleTeamFolderPicked(e.target.files);
                            e.target.value = '';
                          }}
                        />
                      </>
                    )}
                    {isTeamScope && canAdminCurrent && currentTeam && (
                      <Button
                        icon={<SafetyOutlined />}
                        onClick={() => setPermsModalTeamId(currentTeam.team_id)}
                      >
                        {t('管理成员权限')}
                      </Button>
                    )}
                    {isTeamScope && !canEditCurrent && (
                      <span className="jx-team-workspace-readOnly jx-anim-statusIn">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" style={{ marginRight: 6 }}>
                          <path d="M12 15a2 2 0 100-4 2 2 0 000 4z" fill="currentColor" />
                          <path d="M6 10V8a6 6 0 1112 0v2h1a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2v-8a2 2 0 012-2h1zm2 0h8V8a4 4 0 00-8 0v2z" fill="currentColor" />
                        </svg>
                        {t('仅可查看')}
                      </span>
                    )}
                  </div>
                </div>

                <UploadProgressBar progress={teamFolderUploading} />

                <AnimatePresence mode="popLayout" initial={false}>
                  <motion.div
                    key={`${scopeCacheKey(selectedScope)}:${assetFilter}`}
                    className="jx-mySpace-scopeSlide"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1, transition: { duration: 0.22, ease: EASE.standard } }}
                    exit={{ opacity: 0, transition: { duration: 0.16, ease: EASE.exit } }}
                  >
                {selectedScope.kind !== 'team' || !selectedScope.teamId ? (
                  <div className="jx-team-workspace-hint">
                    <div className="jx-team-workspace-hint-icon">
                      <svg viewBox="0 0 48 48" width="48" height="48" fill="none">
                        <path d="M8 16c0-4 3-7 7-7h4l4 4h12c4 0 7 3 7 7v16c0 4-3 7-7 7H15c-4 0-7-3-7-7V16Z"
                              stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
                        <path d="M16 22h16M16 28h10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                      </svg>
                    </div>
                    <div className="jx-team-workspace-hint-title">
                      {myTeams.length === 0 ? t('你还未加入任何团队') : t('请在左侧选择一个团队或文件夹')}
                    </div>
                    <div className="jx-team-workspace-hint-desc">
                      {myTeams.length === 0
                        ? t('被邀请加入团队后，这里会显示团队共享的文件与文件夹。')
                        : t('选择后即可浏览、上传或管理团队文件资产。')}
                    </div>
                  </div>
                ) : teamSkeletonVisible ? (
                  <MySpaceSkeleton tab="assets" assetFilter={assetFilter} />
                ) : loading && resources.length === 0 ? null : resources.length === 0 ? (
                  <div className="jx-mySpace-empty">
                    <div className="jx-mySpace-emptyIcon">
                      <svg viewBox="0 0 48 48" width="48" height="48" fill="none">
                        <rect x="8" y="6" width="32" height="36" rx="4" stroke="var(--color-fill-deep)" strokeWidth="2" />
                        <path d="M16 18h16M16 26h10" stroke="var(--color-fill-deep)" strokeWidth="2" strokeLinecap="round" />
                      </svg>
                    </div>
                    <div className="jx-mySpace-emptyText">{t('当前文件夹暂无文件')}</div>
                  </div>
                ) : (
                  <>
                    {assetFilter === 'document' ? (
                      <DocumentList
                        items={resources}
                        onDownload={handleDownload}
                        onDelete={canEditCurrent ? handleDelete : undefined}
                        onPreview={handlePreview}
                        onAddToKb={openKbPicker}
                        scopeKind="team"
                        canEditCurrent={canEditCurrent}
                        canAdminCurrent={canAdminCurrent}
                      />
                    ) : (
                      <ImageGrid
                        items={resources}
                        onDownload={handleDownload}
                        onDelete={canEditCurrent ? handleDelete : undefined}
                      />
                    )}

                    {loading && resources.length > 0 && (
                      <div className="jx-mySpace-loadMore">
                        <div className="jx-skeletonBlock jx-mySpace-skLoadMore" />
                      </div>
                    )}
                    {!loading && hasMore && resources.length > 0 && (
                      <div className="jx-mySpace-loadMore">
                        <Button onClick={() => void loadMore()}>{t('加载更多')}</Button>
                      </div>
                    )}
                    {!loading && !hasMore && resources.length > 0 && (
                      <div className="jx-mySpace-noMore">{t('已加载全部内容')}</div>
                    )}
                  </>
                )}
                  </motion.div>
                </AnimatePresence>
              </section>
            </div>
          ) : tab === 'assets' && assetScope === 'personal' ? (
            <AnimatePresence
              mode="popLayout"
              custom={navDirRef.current}
              initial={false}
              onExitComplete={() => { navDirRef.current = 0; }}
            >
              <motion.div
                key={`${scopeCacheKey(selectedScope)}:${assetFilter}`}
                className="jx-mySpace-scopeSlide"
                custom={navDirRef.current}
                variants={scopeSlideVariants}
                initial="enter"
                animate="center"
                exit="exit"
              >
            {(() => {
              const showFolders = assetFilter === 'document';
              const folderCount = showFolders ? personalChildFolders.length : 0;
              return currentSkeletonVisible ? (
                <MySpaceSkeleton tab={tab} assetFilter={assetFilter} />
              ) : loading && resources.length === 0 && folderCount === 0 ? null
              : resources.length === 0 && folderCount === 0 ? (
              <div className="jx-mySpace-empty">
                <div className="jx-mySpace-emptyIcon">
                  <svg viewBox="0 0 48 48" width="48" height="48" fill="none">
                    <rect x="8" y="6" width="32" height="36" rx="4" stroke="var(--color-fill-deep)" strokeWidth="2" />
                    <path d="M16 18h16M16 26h10" stroke="var(--color-fill-deep)" strokeWidth="2" strokeLinecap="round" />
                  </svg>
                </div>
                <div className="jx-mySpace-emptyText">{t('当前文件夹暂无内容，先新建文件夹或上传文件试试')}</div>
              </div>
            ) : (
              <>
                {assetFilter === 'document' ? (
                  <DocumentList
                    items={resources}
                    onDownload={handleDownload}
                    onNavigate={handleNavigate}
                    onDelete={handleDelete}
                    onPreview={handlePreview}
                    onAddToKb={openKbPicker}
                    scopeKind="personal"
                    onBulkMoveToTeam={(items) => openMoveToTeam(items.map((i) => i.id))}
                    onMoveToTeam={(item) => openMoveToTeam([item.id])}
                    onCopyToTeam={(item) => openCopyToTeam([item.id])}
                    onBulkCopyToTeam={(items) => openCopyToTeam(items.map((i) => i.id))}
                    folders={personalChildFolders}
                    onEnterFolder={handleEnterFolder}
                    onRenameFolder={handleRenameFolder}
                    onDeleteFolder={handleDeletePersonalFolder}
                    onCopyFolderToTeam={(folderId) => openCopyFolderToTeam(folderId)}
                    onMoveToPersonalFolder={(item) => openMoveToPersonalFolder([item.id])}
                    onBulkMoveToPersonalFolder={(items) => openMoveToPersonalFolder(items.map((i) => i.id))}
                    onCopyToPersonalFolder={(item) => openCopyToPersonalFolder([item.id])}
                    onBulkCopyToPersonalFolder={(items) => openCopyToPersonalFolder(items.map((i) => i.id))}
                  />
                ) : (
                  <ImageGrid
                    items={resources}
                    onDownload={handleDownload}
                    onNavigate={handleNavigate}
                    onDelete={handleDelete}
                    onMoveToTeam={(item) => openMoveToTeam([item.id])}
                    onMoveToPersonalFolder={(item) => openMoveToPersonalFolder([item.id])}
                    onCopyToPersonalFolder={(item) => openCopyToPersonalFolder([item.id])}
                  />
                )}

                {loading && resources.length > 0 && (
                  <div className="jx-mySpace-loadMore">
                    <div className="jx-skeletonBlock jx-mySpace-skLoadMore" />
                  </div>
                )}
                {!loading && hasMore && resources.length > 0 && (
                  <div className="jx-mySpace-loadMore">
                    <Button onClick={() => void loadMore()}>{t('加载更多')}</Button>
                  </div>
                )}
                {!loading && !hasMore && resources.length > 0 && (
                  <div className="jx-mySpace-noMore">{t('已加载全部内容')}</div>
                )}
              </>
            );
            })()}
              </motion.div>
            </AnimatePresence>
          ) : currentSkeletonVisible ? (
            <MySpaceSkeleton tab={tab} assetFilter={assetFilter} />
          ) : loading && currentItems.length === 0 ? null : currentItems.length === 0 ? (
            <div className="jx-mySpace-empty">
              <div className="jx-mySpace-emptyIcon">
                <svg viewBox="0 0 48 48" width="48" height="48" fill="none">
                  <rect x="8" y="6" width="32" height="36" rx="4" stroke="var(--color-fill-deep)" strokeWidth="2" />
                  <path d="M16 18h16M16 26h10" stroke="var(--color-fill-deep)" strokeWidth="2" strokeLinecap="round" />
                </svg>
              </div>
              <div className="jx-mySpace-emptyText">{t('暂无内容')}</div>
            </div>
          ) : (
            <>
              {tab === 'favorites' && (
                <FavoriteList
                  items={currentItems}
                  onNavigate={handleNavigate}
                  onRequestUnfavorite={handleRequestUnfavorite}
                  onFinalizeUnfavorite={handleFinalizeUnfavorite}
                />
              )}

              {loading && currentItems.length > 0 && (
                <div className="jx-mySpace-loadMore">
                  <div className="jx-skeletonBlock jx-mySpace-skLoadMore" />
                </div>
              )}
              {!loading && currentHasMore && currentItems.length > 0 && (
                <div className="jx-mySpace-loadMore">
                  <Button onClick={() => void loadMore()}>{t('加载更多')}</Button>
                </div>
              )}
              {!loading && !currentHasMore && currentItems.length > 0 && (
                <div className="jx-mySpace-noMore">{t('已加载全部内容')}</div>
              )}
            </>
          )}
          </motion.div>
        </div>
      </div>

      {/* Drag-and-drop upload highlight layer (shared component, mounted only while dragging) */}
      <DropOverlay
        active={dragActive && canDropUpload}
        hint={selectedScope.kind === 'team'
          ? t('松开，上传到当前团队文件夹')
          : t('松开，上传到当前文件夹')}
      />

      <MoveToTeamModal
        open={moveModalOpen}
        onClose={() => { setMoveModalOpen(false); setMoveArtifactIds([]); }}
        personalArtifactIds={moveArtifactIds}
      />

      <MoveToTeamModal
        open={copyModalOpen}
        mode="copy"
        onClose={() => { setCopyModalOpen(false); setCopyArtifactIds([]); setCopyFolderId(undefined); }}
        personalArtifactIds={copyArtifactIds}
        personalFolderId={copyFolderId}
      />

      <CreatePersonalFolderModal
        open={createPersonalFolderOpen}
        onClose={() => setCreatePersonalFolderOpen(false)}
      />

      <MoveToPersonalFolderModal
        open={movePersonalModalOpen}
        mode={movePersonalMode}
        onClose={() => { setMovePersonalModalOpen(false); setMovePersonalArtifactIds([]); }}
        artifactIds={movePersonalArtifactIds}
      />

      <TeamPermissionsModal
        open={!!permsModalTeamId}
        teamId={permsModalTeamId}
        onClose={() => setPermsModalTeamId(null)}
      />
      <Modal
        title={t('加入私有知识库')}
        open={kbPickerOpen}
        onCancel={closeKbPicker}
        footer={[
          <Button key="cancel" onClick={closeKbPicker} disabled={kbPickerLoading}>{t('取消')}</Button>,
          <Button key="submit" type="primary" loading={kbPickerLoading} onClick={() => void handleAddToKb()}>
            {t('确认加入')}
          </Button>,
        ]}
      >
        <div className="jx-mySpace-kbPicker">
          <div className="jx-mySpace-kbPickerText">
            {pendingResources.length > 1
              ? t('选择一个或多个私有知识库，用于收录已选的 {n} 个文件', { n: pendingResources.length })
              : pendingResources[0]
                ? t('选择一个或多个私有知识库，用于收录文件“{name}”', { name: pendingResources[0].name })
                : t('请选择目标私有知识库')}
          </div>
          <Select
            className="jx-mySpace-kbPickerSelect"
            mode="multiple"
            value={selectedKbIds}
            onChange={setSelectedKbIds}
            placeholder={t('请选择一个或多个私有知识库')}
            options={privateKbOptions.map((item) => ({ value: item.id, label: item.name }))}
          />
        </div>
      </Modal>
    </div>
  );
}
