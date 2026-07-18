import { create } from 'zustand';
import type { AutomationTask } from '../types';
import {
  listAutomations,
  deleteAutomation as deleteAutomationApi,
  pauseAutomation as pauseApi,
  resumeAutomation as resumeApi,
  triggerAutomation as triggerApi,
  updateAutomation as updateApi,
  type UpdateAutomationRequest,
} from '../api';

interface AutomationState {
  tasks: AutomationTask[];
  loading: boolean;
  createModalOpen: boolean;
  selectedTaskId: string | null;

  setTasks: (tasks: AutomationTask[]) => void;
  setLoading: (v: boolean) => void;
  setCreateModalOpen: (v: boolean) => void;
  setSelectedTaskId: (id: string | null) => void;

  fetchTasks: () => Promise<void>;
  removeTask: (taskId: string) => Promise<void>;
  togglePause: (task: AutomationTask) => Promise<void>;
  triggerNow: (taskId: string) => Promise<void>;
  updateTask: (taskId: string, data: UpdateAutomationRequest) => Promise<AutomationTask>;
  reset: () => void;
}

export const useAutomationStore = create<AutomationState>((set, get) => ({
  tasks: [],
  loading: false,
  createModalOpen: false,
  selectedTaskId: null,

  setTasks: (tasks) => set({ tasks }),
  setLoading: (loading) => set({ loading }),
  setCreateModalOpen: (v) => set({ createModalOpen: v }),
  setSelectedTaskId: (id) => set({ selectedTaskId: id }),

  fetchTasks: async () => {
    set({ loading: true });
    try {
      const tasks = await listAutomations();
      set({ tasks });
    } catch (e) {
      console.error('Failed to fetch automations:', e);
    } finally {
      set({ loading: false });
    }
  },

  removeTask: async (taskId) => {
    try {
      await deleteAutomationApi(taskId);
      set((s) => ({ tasks: s.tasks.filter((t) => t.task_id !== taskId) }));
    } catch (e) {
      console.error('Failed to delete automation:', e);
      throw e;
    }
  },

  togglePause: async (task) => {
    try {
      if (task.status === 'active') {
        await pauseApi(task.task_id);
      } else if (task.status === 'paused') {
        await resumeApi(task.task_id);
      }
      await get().fetchTasks();
    } catch (e) {
      console.error('Failed to toggle automation:', e);
      throw e;
    }
  },

  triggerNow: async (taskId) => {
    try {
      await triggerApi(taskId);
    } catch (e) {
      console.error('Failed to trigger automation:', e);
      throw e;
    }
  },

  updateTask: async (taskId, data) => {
    const updated = await updateApi(taskId, data);
    set((s) => ({ tasks: s.tasks.map((t) => (t.task_id === taskId ? updated : t)) }));
    return updated;
  },

  reset: () => set({ tasks: [], loading: false, createModalOpen: false, selectedTaskId: null }),
}));
