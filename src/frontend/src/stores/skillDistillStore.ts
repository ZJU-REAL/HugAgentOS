import { create } from 'zustand';
import {
  cancelSkillDistillJob,
  createSkillDistillJob,
  deleteSkillDistillJob,
  getSkillDistillJob,
  listSkillDistillJobs,
  type SkillDistillJob,
} from '../api';

interface SkillDistillState {
  jobs: SkillDistillJob[];
  loading: boolean;
  creating: boolean;
  /** The job whose artifact detail is currently being viewed (includes result_skill_content) */
  detailJob: SkillDistillJob | null;
  detailLoading: boolean;

  fetchJobs: () => Promise<void>;
  /** For polling: silent refresh (does not flash loading). Returns whether there are still in-progress jobs. */
  refreshJobs: () => Promise<boolean>;
  createJob: (params: {
    chat_ids: string[] | 'all';
    hint?: string;
    include_project_memories?: boolean;
  }) => Promise<SkillDistillJob>;
  openDetail: (jobId: string) => Promise<void>;
  closeDetail: () => void;
  cancelJob: (jobId: string) => Promise<void>;
  removeJob: (jobId: string) => Promise<void>;
  /** Called by the component after a successful save to sync local state */
  applySavedJob: (job: SkillDistillJob) => void;
}

const hasActive = (jobs: SkillDistillJob[]) =>
  jobs.some((j) => j.status === 'queued' || j.status === 'running');

export const useSkillDistillStore = create<SkillDistillState>((set, get) => ({
  jobs: [],
  loading: false,
  creating: false,
  detailJob: null,
  detailLoading: false,

  fetchJobs: async () => {
    set({ loading: true });
    try {
      const jobs = await listSkillDistillJobs();
      set({ jobs });
    } catch (e) {
      console.error('Failed to fetch skill distill jobs:', e);
    } finally {
      set({ loading: false });
    }
  },

  refreshJobs: async () => {
    try {
      const jobs = await listSkillDistillJobs();
      set({ jobs });
      return hasActive(jobs);
    } catch (e) {
      console.error('Failed to refresh skill distill jobs:', e);
      return hasActive(get().jobs);
    }
  },

  createJob: async (params) => {
    set({ creating: true });
    try {
      const job = await createSkillDistillJob(params);
      set({ jobs: [job, ...get().jobs] });
      return job;
    } finally {
      set({ creating: false });
    }
  },

  openDetail: async (jobId) => {
    set({ detailLoading: true, detailJob: null });
    try {
      const job = await getSkillDistillJob(jobId);
      set({ detailJob: job });
    } finally {
      set({ detailLoading: false });
    }
  },

  closeDetail: () => set({ detailJob: null }),

  cancelJob: async (jobId) => {
    const job = await cancelSkillDistillJob(jobId);
    set({ jobs: get().jobs.map((j) => (j.job_id === jobId ? { ...j, ...job } : j)) });
  },

  removeJob: async (jobId) => {
    await deleteSkillDistillJob(jobId);
    set({
      jobs: get().jobs.filter((j) => j.job_id !== jobId),
      detailJob: get().detailJob?.job_id === jobId ? null : get().detailJob,
    });
  },

  applySavedJob: (job) => {
    set({
      jobs: get().jobs.map((j) => (j.job_id === job.job_id ? { ...j, ...job } : j)),
      detailJob:
        get().detailJob?.job_id === job.job_id
          ? { ...get().detailJob!, ...job }
          : get().detailJob,
    });
  },
}));
