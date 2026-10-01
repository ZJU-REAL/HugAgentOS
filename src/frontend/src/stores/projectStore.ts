import { create } from 'zustand';

import type { UploadProgress } from '../components/common/UploadProgressBar';
import type {
  ProjectChatSummary,
  ProjectDetail,
  ProjectFileItem,
  ProjectItem,
  ProjectKind,
} from '../types';
import {
  createProject as apiCreateProject,
  deleteProject as apiDeleteProject,
  getProject as apiGetProject,
  listProjectChats,
  listProjectFiles,
  listProjects,
  removeProjectFile,
  toggleProjectFavorite,
  updateProject as apiUpdateProject,
  updateProjectInstructions as apiUpdateProjectInstructions,
  uploadProjectFile,
} from '../api';
import { t } from '../i18n';

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
  list: ProjectItem[];
  listLoading: boolean;
  searchKeyword: string;
  sort: SortKey;
  listError: string | null;
  total: number;
  currentProjectId: string | null;
  currentProject: ProjectDetail | null;
  detailLoading: boolean;
  projectFiles: ProjectFileItem[];
  filesLoading: boolean;
  filesError: string | null;
  projectChats: ProjectChatSummary[];
  capacityUsed: number;
  capacityLimit: number;
  uploadProgress: UploadProgress | null;
  createModalOpen: boolean;
  referenceModalOpen: boolean;
  instructionsEditOpen: boolean;
  setSearchKeyword: (q: string) => void;
  setSort: (s: SortKey) => void;
  fetchProjects: () => Promise<void>;
  resetProjectList: () => void;
  openProject: (projectId: string) => Promise<void>;
  reloadProject: (projectId: string) => Promise<void>;
  closeCurrentProject: () => void;
  createPersonal: (name: string, description?: string, linkedFolderId?: string) => Promise<string>;
  updateProject: (patch: { name?: string; description?: string; pinned?: boolean; icon_color?: string; memory_enabled?: boolean; memory_write_enabled?: boolean }) => Promise<void>;
  updateInstructions: (instructions: string, revision?: string) => Promise<void>;
  refreshInstructions: () => Promise<void>;
  deleteProject: (projectId: string) => Promise<void>;
  toggleFavorite: (on: boolean) => Promise<void>;
  toggleFavoriteById: (projectId: string, on: boolean) => Promise<void>;
  togglePinnedById: (projectId: string, on: boolean) => Promise<void>;
  refreshFiles: () => Promise<void>;
  uploadFile: (file: File) => Promise<void>;
  uploadFiles: (files: File[]) => Promise<{ succeeded: number; failed: number }>;
  removeFile: (artifactId: string) => Promise<void>;
  refreshChats: (scope?: 'all' | 'mine' | 'shared') => Promise<void>;
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
  projectChats: [],
  capacityUsed: 0,
  capacityLimit: 0,
  uploadProgress: null,
  createModalOpen: false,
  referenceModalOpen: false,
  instructionsEditOpen: false,

  setSearchKeyword: (q) => set({ searchKeyword: q }),
  setSort: (sort) => set({ sort }),

  resetProjectList: () => {
    projectListGeneration += 1;
    set({ list: [], total: 0, listLoading: false, listError: null, searchKeyword: '' });
  },

  fetchProjects: async () => {
    const generation = ++projectListGeneration;
    const { searchKeyword, sort } = get();
    set({ listLoading: true, listError: null });
    try {
      const result = await listProjects({
        q: searchKeyword.trim() || undefined,
        sort: SORT_MAP[sort],
      });
      if (generation !== projectListGeneration) return;
      set({
        list: result.items,
        total: result.pagination?.total_items || result.items.length,
      });
    } catch (error) {
      if (generation !== projectListGeneration) return;
      set({ listError: (error as Error).message || t('加载失败') });
    } finally {
      if (generation === projectListGeneration) set({ listLoading: false });
    }
  },

  openProject: async (projectId) => {
    const loading = get().reloadProject(projectId);
    await loading;
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
        currentProject: null, projectFiles: [], projectChats: [],
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
        const results = await Promise.allSettled([get().refreshFiles(), get().refreshChats()]);
        for (const result of results) {
          if (result.status === 'rejected') console.warn('Project resource load failed', result.reason);
        }
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
      projectChats: [],
      capacityUsed: 0,
      capacityLimit: 0,
      instructionsEditOpen: false,
    });
  },

  createPersonal: async (name, description, linkedFolderId) => {
    const project = await apiCreateProject({
      name,
      description,
      kind: 'personal',
      linked_folder_id: linkedFolderId,
    });
    await get().fetchProjects();
    return project.project_id;
  },

  updateProject: async (patch) => {
    const { currentProjectId, currentProject } = get();
    if (!currentProjectId) return;
    const updated = await apiUpdateProject(currentProjectId, patch);
    set({ currentProject: { ...currentProject, ...updated } });
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

  refreshInstructions: async () => {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    const before = get().currentProject;
    const updated = await apiGetProject(currentProjectId);
    if (get().currentProjectId !== currentProjectId || get().currentProject !== before) return;
    set({ currentProject: updated });
  },

  deleteProject: async (projectId) => {
    await apiDeleteProject(projectId);
    if (get().currentProjectId === projectId) get().closeCurrentProject();
    await get().fetchProjects();
  },

  toggleFavorite: async (on) => {
    const { currentProjectId, currentProject } = get();
    if (!currentProjectId) return;
    await toggleProjectFavorite(currentProjectId, on);
    if (currentProject) set({ currentProject: { ...currentProject, favorite: on } });
    set({
      list: get().list.map((project) => (
        project.project_id === currentProjectId ? { ...project, favorite: on } : project
      )),
    });
  },

  toggleFavoriteById: async (projectId, on) => {
    const applyFavorite = (value: boolean) => {
      set({
        list: get().list.map((project) => (
          project.project_id === projectId ? { ...project, favorite: value } : project
        )),
      });
      const { currentProjectId, currentProject } = get();
      if (currentProject && currentProjectId === projectId) {
        set({ currentProject: { ...currentProject, favorite: value } });
      }
    };
    applyFavorite(on);
    try {
      await toggleProjectFavorite(projectId, on);
    } catch (error) {
      applyFavorite(!on);
      console.warn('toggleFavoriteById failed', projectId, error);
    }
  },

  togglePinnedById: async (projectId, on) => {
    const applyPinned = (value: boolean) => {
      set({
        list: get().list.map((project) => (
          project.project_id === projectId ? { ...project, pinned: value } : project
        )),
      });
      const { currentProjectId, currentProject } = get();
      if (currentProject && currentProjectId === projectId) {
        set({ currentProject: { ...currentProject, pinned: value } });
      }
    };

    applyPinned(on);
    try {
      await apiUpdateProject(projectId, { pinned: on });
    } catch (error) {
      applyPinned(!on);
      throw error;
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
      for (const file of files) {
        try {
          await uploadProjectFile(currentProjectId, file);
          succeeded += 1;
        } catch (error) {
          console.warn('uploadFiles single failed', file.name, error);
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

  refreshChats: async (scope) => {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    const generation = projectGeneration;
    const { items } = await listProjectChats(currentProjectId, 1, 50, scope || 'all');
    if (generation !== projectGeneration || get().currentProjectId !== currentProjectId) return;
    set({ projectChats: items });
  },

  setCreateModalOpen: (createModalOpen) => set({ createModalOpen }),
  setReferenceModalOpen: (referenceModalOpen) => set({ referenceModalOpen }),
  setInstructionsEditOpen: (instructionsEditOpen) => set({ instructionsEditOpen }),
}));

export type { SortKey, ProjectKind };
