import { create } from 'zustand';
import type { UploadProgress } from '../components/common/UploadProgressBar';
import type {
  ProjectDetail,
  ProjectFileItem,
  ProjectItem,
  ProjectKind,
} from '../types';
import {
  createProject as apiCreateProject,
  deleteProject as apiDeleteProject,
  getProject as apiGetProject,
  listProjects,
  listProjectFiles,
  removeProjectFile,
  toggleProjectFavorite,
  updateProject as apiUpdateProject,
  updateProjectInstructions as apiUpdateProjectInstructions,
  uploadProjectFile,
} from '../api';
import { registerUnboundProject } from '../storage';
import { isAddressableChat, isLocalDraftChat, useChatStore } from './chatStore';

import { t } from '../i18n';

let projectSelectionGeneration = 0;
let projectGeneration = 0;
let projectListGeneration = 0;
let pendingProject: { id: string; generation: number; promise: Promise<void> } | null = null;

type SortKey = 'activity' | 'name' | 'created';

const SORT_MAP: Record<SortKey, string> = {
  activity: '-last_activity_at',
  name: 'name',
  created: 'created',
};

interface ProjectStoreState {
  // List state
  list: ProjectItem[];
  listLoading: boolean;
  searchKeyword: string;
  sort: SortKey;
  listError: string | null;
  total: number;

  // Detail state
  currentProjectId: string | null;
  currentProject: ProjectDetail | null;
  detailLoading: boolean;
  projectFiles: ProjectFileItem[];
  filesLoading: boolean;
  filesError: string | null;
  capacityUsed: number;
  capacityLimit: number;
  /** Batch upload progress (non-null during uploadFiles), consumed by the right-column progress bar */
  uploadProgress: UploadProgress | null;

  // Dialog state
  createModalOpen: boolean;
  referenceModalOpen: boolean;
  instructionsEditOpen: boolean;


  // ── Actions ──
  setSearchKeyword: (q: string) => void;
  setSort: (s: SortKey) => void;
  fetchProjects: () => Promise<void>;
  resetProjectList: () => void;
  /** 选择项目并进入绑定该项目的统一会话界面。 */
  openProject: (projectId: string) => Promise<void>;
  /** 只重新载入详情，不跳转。给「已经在这个项目页上」的刷新场景用。 */
  reloadProject: (projectId: string) => Promise<void>;
  closeCurrentProject: () => void;


  createPersonal: (name: string, description?: string, linkedFolderId?: string) => Promise<string>;

  updateProject: (patch: Parameters<typeof apiUpdateProject>[1]) => Promise<void>;
  updateInstructions: (instructions: string, revision?: string) => Promise<void>;
  refreshInstructions: () => Promise<void>;
  deleteProject: (projectId: string) => Promise<void>;
  toggleFavorite: (on: boolean) => Promise<void>;
  /** List-page star optimistic update: flip the UI first, roll back if the request fails (does not open project detail) */
  toggleFavoriteById: (projectId: string, on: boolean) => Promise<void>;
  /** Sidebar pin optimistic update: reorder immediately, roll back if the request fails. */
  togglePinnedById: (projectId: string, on: boolean) => Promise<void>;

  refreshFiles: () => Promise<void>;
  uploadFile: (file: File) => Promise<void>;
  /** Batch upload (including the local folder webkitdirectory case). Returns { succeeded, failed } */
  uploadFiles: (files: File[]) => Promise<{ succeeded: number; failed: number }>;
  /** Delete a project file (effectively a soft-delete of that MySpace artifact) */
  removeFile: (artifactId: string) => Promise<void>;


  setCreateModalOpen: (v: boolean) => void;
  setReferenceModalOpen: (v: boolean) => void;
  setInstructionsEditOpen: (v: boolean) => void;
}

export const useProjectStore = create<ProjectStoreState>((set, get) => ({
  list: [],
  listLoading: false,
  searchKeyword: '',
  sort: 'activity',
  listError: null,
  total: 0,

  currentProjectId: null,
  currentProject: null,
  detailLoading: false,
  projectFiles: [],
  filesLoading: false,
  filesError: null,
  capacityUsed: 0,
  capacityLimit: 0,
  uploadProgress: null,

  createModalOpen: false,
  referenceModalOpen: false,
  instructionsEditOpen: false,


  setSearchKeyword: (q) => set({ searchKeyword: q }),
  setSort: (s) => set({ sort: s }),

  resetProjectList: () => {
    projectListGeneration += 1;
    projectSelectionGeneration += 1;
    set({ list: [], total: 0, listLoading: false, listError: null, searchKeyword: '' });
  },

  fetchProjects: async () => {
    const generation = ++projectListGeneration;
    const { searchKeyword, sort } = get();
    set({ listLoading: true, listError: null });
    try {
      const r = await listProjects({ q: searchKeyword.trim() || undefined, sort: SORT_MAP[sort] });
      if (generation !== projectListGeneration) return;
      set({ list: r.items, total: r.pagination?.total_items || r.items.length });
    } catch (err) {
      if (generation !== projectListGeneration) return;
      set({ listError: (err as Error).message || t('加载失败') });
    } finally {
      if (generation === projectListGeneration) set({ listLoading: false });
    }
  },

  openProject: async (projectId) => {
    const selection = ++projectSelectionGeneration;
    const chat = useChatStore.getState();
    const userId = chat.currentUserId;
    const originPath = window.location.pathname;
    const originChatId = chat.currentChatId;
    const project = get().list.find(item => item.project_id === projectId) || await apiGetProject(projectId);
    if (selection !== projectSelectionGeneration || useChatStore.getState().currentUserId !== userId
      || window.location.pathname !== originPath || useChatStore.getState().currentChatId !== originChatId) return;
    if (chat.currentChat()?.projectId !== projectId || chat.currentChat()?.automationTaskId || isAddressableChat(chat.currentChatId)) {
      const draft = Object.values(chat.store.chats).find(item =>
        item.projectId === projectId && !item.automationTaskId && isLocalDraftChat(item.id));
      if (draft) {
        chat.bindChatProject(draft.id, projectId, project.name);
        chat.setCurrentChatId(draft.id);
      }
      else {
        chat.newChat({ projectId, projectName: project.name });
      }
    } else {
      chat.bindChatProject(chat.currentChatId, projectId, project.name);
      chat.setCurrentChatId(chat.currentChatId);
    }
    chat.setToolResultPanel(null);
  },

  reloadProject: (projectId) => {
    if (pendingProject?.id === projectId && pendingProject.generation === projectGeneration) {
      return pendingProject.promise;
    }
    const generation = ++projectGeneration;
    const switched = get().currentProject?.project_id !== projectId;
    set({
      currentProjectId: projectId, detailLoading: true,
      ...(switched ? {
        currentProject: null, projectFiles: [],
        capacityUsed: 0, capacityLimit: 0, filesLoading: false, filesError: null,
      } : {}),
    });
    const promise = (async () => {
      try {
        const proj = await apiGetProject(projectId);
        if (generation !== projectGeneration) return;
        set({
          currentProject: proj,
          capacityUsed: proj.capacity_used ?? 0,
          capacityLimit: proj.capacity_limit ?? 0,
          detailLoading: false,
        });
        // A file-list failure should not hide valid project details or instructions.
        await get().refreshFiles().catch(() => { /* refreshFiles exposes the error in filesError. */ });
      } catch (err) {
        if (generation !== projectGeneration) return;
        console.warn('openProject failed', err);
        set({ currentProject: null });
      } finally {
        if (generation === projectGeneration) {
          set({ detailLoading: false });
          pendingProject = null;
        }
      }
    })();
    pendingProject = { id: projectId, generation, promise };
    return promise;
  },

  closeCurrentProject: () => {
    projectGeneration += 1;
    pendingProject = null;
    set({
      currentProjectId: null,
      currentProject: null,
      detailLoading: false,
      projectFiles: [],
      filesLoading: false,
      filesError: null,
      capacityUsed: 0,
      capacityLimit: 0,
      instructionsEditOpen: false,
    });
  },

  createPersonal: async (name, description, linkedFolderId) => {
    const proj = await apiCreateProject({
      name,
      description,
      kind: 'personal',
      linked_folder_id: linkedFolderId,
    });
    await get().fetchProjects();
    return proj.project_id;
  },

  updateProject: async (patch) => {
    const { currentProjectId, currentProject } = get();
    if (!currentProjectId) return;
    const updated = await apiUpdateProject(currentProjectId, patch);
    set({ currentProject: { ...currentProject, ...updated } });
    // Keep the list card in sync so a rename shows up without a refetch.
    set({
      list: get().list.map((p) =>
        p.project_id === currentProjectId
          ? { ...p, name: updated.name, description: updated.description }
          : p,
      ),
    });
    // Sessions cache the project name locally (chatStore.chats[].projectName) — refresh those
    // labels too, otherwise the sidebar keeps showing the old name until a reload.
    if (patch.name !== undefined) {
      const { useChatStore } = await import('./chatStore');
      useChatStore.getState().updateStore((prev) => ({
        ...prev,
        chats: Object.fromEntries(
          Object.entries(prev.chats).map(([id, chat]) => [
            id,
            chat.projectId === currentProjectId ? { ...chat, projectName: updated.name } : chat,
          ]),
        ),
      }));
    }
  },

  refreshInstructions: async () => {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    const before = get().currentProject;
    const updated = await apiGetProject(currentProjectId);
    if (get().currentProjectId !== currentProjectId || get().currentProject !== before) return;
    set({ currentProject: updated });
  },

  updateInstructions: async (instructions, revision) => {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    const updated = await apiUpdateProjectInstructions(currentProjectId, instructions, revision);
    if (get().currentProjectId !== currentProjectId) return;
    set({ currentProject: updated });
    // The PATCH response already contains the saved instructions and revision.
    // A failed file-list refresh must not report a successful save as a failure.
    void get().refreshFiles().catch(() => {});
  },

  deleteProject: async (projectId) => {
    await apiDeleteProject(projectId);
    // 登记成"已删除项目"：会话上残留的 projectId/projectName 会在合并写盘和
    // 下次加载时被摘掉，别的窗口也不会再把旧绑定贴回来（否则侧边栏会拿
    // projectName 兜底造出一个已删除项目的分组，新对话就挂在它下面）。
    registerUnboundProject(useChatStore.getState().currentUserId, projectId);
    useChatStore.getState().updateStore((prev) => {
      let changed = false;
      const chats = { ...prev.chats };
      for (const [chatId, chat] of Object.entries(chats)) {
        if (chat.projectId !== projectId) continue;
        const next = { ...chat };
        delete next.projectId;
        delete next.projectName;
        chats[chatId] = next;
        changed = true;
      }
      return changed ? { ...prev, chats } : prev;
    });
    if (get().currentProjectId === projectId) {
      get().closeCurrentProject();
    }
    await get().fetchProjects();
  },

  toggleFavorite: async (on) => {
    const { currentProjectId, currentProject } = get();
    if (!currentProjectId) return;
    await toggleProjectFavorite(currentProjectId, on);
    if (currentProject) set({ currentProject: { ...currentProject, favorite: on } });
    // sync list
    set({
      list: get().list.map((p) =>
        p.project_id === currentProjectId ? { ...p, favorite: on } : p,
      ),
    });
  },

  toggleFavoriteById: async (projectId, on) => {
    const applyFavorite = (value: boolean) => {
      set({
        list: get().list.map((p) =>
          p.project_id === projectId ? { ...p, favorite: value } : p,
        ),
      });
      const { currentProjectId, currentProject } = get();
      if (currentProject && currentProjectId === projectId) {
        set({ currentProject: { ...currentProject, favorite: value } });
      }
    };
    // Optimistic update: flip the UI first so the star animation happens immediately
    applyFavorite(on);
    try {
      await toggleProjectFavorite(projectId, on);
    } catch (err) {
      // Roll back on failure
      applyFavorite(!on);
      console.warn('toggleFavoriteById failed', projectId, err);
    }
  },

  togglePinnedById: async (projectId, on) => {
    const applyPinned = (value: boolean) => {
      set({
        list: get().list.map((p) =>
          p.project_id === projectId ? { ...p, pinned: value } : p,
        ),
      });
      const { currentProjectId, currentProject } = get();
      if (currentProject && currentProjectId === projectId) {
        set({ currentProject: { ...currentProject, pinned: value } });
      }
    };

    applyPinned(on);
    try {
      await apiUpdateProject(projectId, { pinned: on });
    } catch (err) {
      applyPinned(!on);
      throw err;
    }
  },

  refreshFiles: async () => {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    const generation = projectGeneration;
    set({ filesLoading: true, filesError: null });
    try {
      const { items, capacity_used, capacity_limit } = await listProjectFiles(currentProjectId);
      if (generation !== projectGeneration || get().currentProjectId !== currentProjectId) return;
      set({
        projectFiles: items,
        capacityUsed: capacity_used ?? 0,
        capacityLimit: capacity_limit ?? 0,
      });
    } catch (err) {
      if (generation === projectGeneration && get().currentProjectId === currentProjectId) {
        set({ filesError: (err as Error).message || t('加载失败') });
      }
      throw err;
    } finally {
      if (generation === projectGeneration && get().currentProjectId === currentProjectId) {
        set({ filesLoading: false });
      }
    }
  },

  uploadFile: async (file) => {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    await uploadProjectFile(currentProjectId, file);
    await get().refreshFiles();
    await get().refreshInstructions();
  },

  uploadFiles: async (files) => {
    const { currentProjectId } = get();
    if (!currentProjectId) return { succeeded: 0, failed: 0 };
    let succeeded = 0;
    let failed = 0;
    set({ uploadProgress: { done: 0, total: files.length } });
    try {
      // Serial upload: preserve the relative path (webkitRelativePath is used as the filename inside uploadProjectFile),
      // control concurrency and avoid instantaneous backend pressure; the backend capacity check compares against cumulative used, so uploads must be serialized in order to avoid over-limit misjudgment.
      for (const f of files) {
        try {
          await uploadProjectFile(currentProjectId, f);
          succeeded += 1;
        } catch (err) {
          console.warn('uploadFiles single failed', (f as File).name, err);
          failed += 1;
        }
        set({ uploadProgress: { done: succeeded + failed, total: files.length } });
      }
      await get().refreshFiles();
    } finally {
      set({ uploadProgress: null });
    }
    return { succeeded, failed };
  },

  removeFile: async (artifactId) => {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    await removeProjectFile(currentProjectId, artifactId);
    await get().refreshFiles();
    await get().refreshInstructions();
  },

  setCreateModalOpen: (v) => set({ createModalOpen: v }),
  setReferenceModalOpen: (v) => set({ referenceModalOpen: v }),
  setInstructionsEditOpen: (v) => set({ instructionsEditOpen: v }),
}));

export type { SortKey, ProjectKind };
