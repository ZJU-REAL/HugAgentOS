import { create } from 'zustand';
import {
  automationAvailabilityWarning,
  deleteAutomation as deleteAutomationApi,
  listAutomations,
  pauseAutomation as pauseApi,
  resumeAutomation as resumeApi,
  triggerAutomation as triggerApi,
  updateAutomation as updateApi,
  type UpdateAutomationRequest,
} from '../api';
import { navigateTo, pathForPanel } from '../routing/navigation';
import type { AutomationTask } from '../types';

interface AutomationState {
  tasks: AutomationTask[];
  loading: boolean;
  availabilityWarning: string;
  createModalOpen: boolean;

  setCreateModalOpen: (v: boolean) => void;
  setSelectedTaskId: (id: string | null) => void;

  fetchTasks: () => Promise<void>;
  removeTask: (taskId: string) => Promise<void>;
  togglePause: (task: AutomationTask) => Promise<void>;
  triggerNow: (taskId: string) => Promise<void>;
  updateTask: (taskId: string, data: UpdateAutomationRequest) => Promise<AutomationTask>;
  reset: () => void;
}

let taskRequestVersion = 0;

export const useAutomationStore = create<AutomationState>((set, get) => ({
  tasks: [],
  loading: false,
  availabilityWarning: '',
  createModalOpen: false,

  setCreateModalOpen: (v) => set({ createModalOpen: v }),
  // 打开 / 关闭任务详情 = 换地址（/automation/<任务id>），不在 store 里另存一份位置
  setSelectedTaskId: (id) => navigateTo(pathForPanel('automation', id)),

  fetchTasks: async () => {
    const version = ++taskRequestVersion;
    set({ loading: true });
    try {
      const tasks = await listAutomations();
      if (version !== taskRequestVersion) return;
      set({ tasks, availabilityWarning: automationAvailabilityWarning() });
    } catch (e) {
      if (version !== taskRequestVersion) return;
      set({ availabilityWarning: e instanceof Error ? e.message : '任务列表暂不可用' });
      console.error('Failed to fetch automations:', e);
    } finally {
      if (version === taskRequestVersion) set({ loading: false });
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

  reset: () => { taskRequestVersion += 1; set({ tasks: [], availabilityWarning: '', loading: false, createModalOpen: false }); },
}));
