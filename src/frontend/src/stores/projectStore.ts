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
  openProject: (projectId: string) => Promise<void>;
  closeCurrentProject: () => void;
  createPersonal: (name: string, description?: string, linkedFolderId?: string) => Promise<string>;
  updateProject: (patch: { name?: string; description?: string; pinned?: boolean; icon_color?: string; memory_enabled?: boolean; memory_write_enabled?: boolean }) => Promise<void>;
  updateInstructions: (instructions: string) => Promise<void>;
  deleteProject: (projectId: string) => Promise<void>;
  toggleFavorite: (on: boolean) => Promise<void>;
  toggleFavoriteById: (projectId: string, on: boolean) => Promise<void>;
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
  projectChats: [],
  capacityUsed: 0,
  capacityLimit: 0,
  uploadProgress: null,
  createModalOpen: false,
  referenceModalOpen: false,
  instructionsEditOpen: false,

  setSearchKeyword: (q) => set({ searchKeyword: q }),
  setSort: (sort) => set({ sort }),

  fetchProjects: async () => {
    const { searchKeyword, sort } = get();
    set({ listLoading: true, listError: null });
    try {
      const result = await listProjects({
        q: searchKeyword.trim() || undefined,
        sort: SORT_MAP[sort],
      });
      set({
        list: result.items,
        total: result.pagination?.total_items || result.items.length,
      });
    } catch (error) {
      set({ listError: (error as Error).message || t('加载失败') });
    } finally {
      set({ listLoading: false });
    }
  },

  openProject: async (projectId) => {
    set({ currentProjectId: projectId, detailLoading: true });
    try {
      const project = await apiGetProject(projectId);
      set({
        currentProject: project,
        capacityUsed: project.capacity_used ?? 0,
        capacityLimit: project.capacity_limit ?? 0,
      });
      await Promise.all([get().refreshFiles(), get().refreshChats()]);
    } catch (error) {
      console.warn('openProject failed', error);
      set({ currentProject: null });
    } finally {
      set({ detailLoading: false });
    }
  },

  closeCurrentProject: () => set({
    currentProjectId: null,
    currentProject: null,
    projectFiles: [],
    projectChats: [],
    capacityUsed: 0,
    capacityLimit: 0,
  }),

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

  updateInstructions: async (instructions) => {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    set({ currentProject: await apiUpdateProjectInstructions(currentProjectId, instructions) });
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

  refreshFiles: async () => {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    const result = await listProjectFiles(currentProjectId);
    set({
      projectFiles: result.items,
      capacityUsed: result.capacity_used,
      capacityLimit: result.capacity_limit,
    });
  },

  uploadFile: async (file) => {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    await uploadProjectFile(currentProjectId, file);
    await get().refreshFiles();
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
  },

  refreshChats: async (scope) => {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    const { items } = await listProjectChats(currentProjectId, 1, 50, scope || 'all');
    set({ projectChats: items });
  },

  setCreateModalOpen: (createModalOpen) => set({ createModalOpen }),
  setReferenceModalOpen: (referenceModalOpen) => set({ referenceModalOpen }),
  setInstructionsEditOpen: (instructionsEditOpen) => set({ instructionsEditOpen }),
}));

export type { SortKey, ProjectKind };
