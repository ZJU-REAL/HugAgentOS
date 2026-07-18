import { create } from 'zustand';
import type { UploadProgress } from '../components/common/UploadProgressBar';
import type {
  ProjectChatSummary,
  ProjectDetail,
  ProjectFileItem,
  ProjectItem,
  ProjectKind,
  TeamForProjectCreation,
} from '../types';
import {
  createProject as apiCreateProject,
  deleteProject as apiDeleteProject,
  getProject as apiGetProject,
  listMyTeamsForProjects,
  listProjects,
  listProjectChats,
  listProjectFiles,
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
  projectChats: ProjectChatSummary[];
  capacityUsed: number;
  capacityLimit: number;
  /** Batch upload progress (non-null during uploadFiles), consumed by the right-column progress bar */
  uploadProgress: UploadProgress | null;

  // Dialog state
  createModalOpen: boolean;
  referenceModalOpen: boolean;
  instructionsEditOpen: boolean;

  // Team dropdown
  availableTeams: TeamForProjectCreation[];

  // ── Actions ──
  setSearchKeyword: (q: string) => void;
  setSort: (s: SortKey) => void;
  fetchProjects: () => Promise<void>;
  openProject: (projectId: string) => Promise<void>;
  closeCurrentProject: () => void;

  loadTeamTargets: () => Promise<void>;

  createPersonal: (name: string, description?: string, linkedFolderId?: string) => Promise<string>;
  createTeam: (teamId: string, name: string, description?: string, linkedTeamFolderId?: string) => Promise<string>;

  updateProject: (patch: { name?: string; description?: string; pinned?: boolean; icon_color?: string; memory_enabled?: boolean; memory_write_enabled?: boolean }) => Promise<void>;
  updateInstructions: (instructions: string) => Promise<void>;
  deleteProject: (projectId: string) => Promise<void>;
  toggleFavorite: (on: boolean) => Promise<void>;
  /** List-page star optimistic update: flip the UI first, roll back if the request fails (does not open project detail) */
  toggleFavoriteById: (projectId: string, on: boolean) => Promise<void>;

  refreshFiles: () => Promise<void>;
  uploadFile: (file: File) => Promise<void>;
  /** Batch upload (including the local folder webkitdirectory case). Returns { succeeded, failed } */
  uploadFiles: (files: File[]) => Promise<{ succeeded: number; failed: number }>;
  /** Delete a project file (effectively a soft-delete of that MySpace artifact) */
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

  availableTeams: [],

  setSearchKeyword: (q) => set({ searchKeyword: q }),
  setSort: (s) => set({ sort: s }),

  fetchProjects: async () => {
    const { searchKeyword, sort } = get();
    set({ listLoading: true, listError: null });
    try {
      const r = await listProjects({ q: searchKeyword.trim() || undefined, sort: SORT_MAP[sort] });
      set({ list: r.items, total: r.pagination?.total_items || r.items.length });
    } catch (err) {
      set({ listError: (err as Error).message || t('加载失败') });
    } finally {
      set({ listLoading: false });
    }
  },

  openProject: async (projectId) => {
    set({ currentProjectId: projectId, detailLoading: true });
    try {
      const proj = await apiGetProject(projectId);
      set({
        currentProject: proj,
        capacityUsed: proj.capacity_used ?? 0,
        capacityLimit: proj.capacity_limit ?? 0,
      });
      await Promise.all([get().refreshFiles(), get().refreshChats()]);
    } catch (err) {
      console.warn('openProject failed', err);
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

  loadTeamTargets: async () => {
    try {
      const teams = await listMyTeamsForProjects();
      set({ availableTeams: teams });
    } catch {
      set({ availableTeams: [] });
    }
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

  createTeam: async (teamId, name, description, linkedTeamFolderId) => {
    const proj = await apiCreateProject({
      name,
      description,
      kind: 'team',
      team_id: teamId,
      linked_team_folder_id: linkedTeamFolderId,
    });
    await get().fetchProjects();
    return proj.project_id;
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
    const updated = await apiUpdateProjectInstructions(currentProjectId, instructions);
    set({ currentProject: updated });
  },

  deleteProject: async (projectId) => {
    await apiDeleteProject(projectId);
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

  refreshFiles: async () => {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    const { items, capacity_used, capacity_limit } = await listProjectFiles(currentProjectId);
    set({
      projectFiles: items,
      capacityUsed: capacity_used,
      capacityLimit: capacity_limit,
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
      // Serial upload: preserve the relative path (webkitRelativePath is used as the filename inside uploadProjectFile),
      // control concurrency and avoid instantaneous backend pressure; the backend capacity check compares against cumulative used, so uploads must be serialized in order to avoid over-limit misjudgment.
      for (const f of files) {
        try {
          await uploadProjectFile(currentProjectId, f);
          succeeded += 1;
        } catch (err) {
          // eslint-disable-next-line no-console
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
  },

  refreshChats: async (scope) => {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    const { items } = await listProjectChats(currentProjectId, 1, 50, scope || 'all');
    set({ projectChats: items });
  },

  setCreateModalOpen: (v) => set({ createModalOpen: v }),
  setReferenceModalOpen: (v) => set({ referenceModalOpen: v }),
  setInstructionsEditOpen: (v) => set({ instructionsEditOpen: v }),
}));

export type { SortKey, ProjectKind };
