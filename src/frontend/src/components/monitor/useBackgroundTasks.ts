import { useCallback, useEffect, useState } from 'react';
import { cancelCommandApi, cancelJobApi, listChatCommands, listChatJobs } from '../../api';
import { useChatStore } from '../../stores/chatStore';
import type { CommandTaskBrief, JobBrief } from '../../types';

const LIVE_COMMAND = new Set(['starting', 'running']);
const LIVE_JOB = new Set(['pending', 'running']);
/** Live tasks change by the second; poll only while something can still move. */
const POLL_MS = 3000;

export interface BackgroundTasks {
  commands: CommandTaskBrief[];
  jobs: JobBrief[];
  cancelCommand: (taskId: string) => Promise<void>;
  cancelJob: (jobId: string) => Promise<void>;
}

export const isLiveCommand = (task: CommandTaskBrief) => LIVE_COMMAND.has(task.status);
export const isLiveJob = (job: JobBrief) => LIVE_JOB.has(job.status);

/**
 * Background commands and batch jobs of a chat.
 *
 * Polls while a task is live or the chat has a run in flight (a run can hand a
 * command to the background at any moment); otherwise it fetches once per chat
 * and again when a run ends.
 */
export function useBackgroundTasks(chatId: string | null): BackgroundTasks {
  // Keyed by chat so switching chats never shows the previous chat's tasks.
  const [state, setState] = useState<{ chatId: string | null; commands: CommandTaskBrief[]; jobs: JobBrief[] }>(
    { chatId: null, commands: [], jobs: [] },
  );
  const [refresh, setRefresh] = useState(0);
  const runActive = useChatStore((s) => !!chatId && (s.sendingChatIds.has(chatId) || !!s.activeRuns[chatId]));
  const commands = state.chatId === chatId ? state.commands : [];
  const jobs = state.chatId === chatId ? state.jobs : [];
  const live = commands.some(isLiveCommand) || jobs.some(isLiveJob);

  useEffect(() => {
    if (!chatId) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      const [nextCommands, nextJobs] = await Promise.all([
        listChatCommands(chatId).catch(() => null),
        listChatJobs(chatId, false).catch(() => null),
      ]);
      if (!active) return;
      setState((prev) => {
        const base = prev.chatId === chatId ? prev : { chatId, commands: [], jobs: [] };
        return {
          chatId,
          // Older backends do not send `background`; their live rows are the background ones.
          commands: nextCommands ? nextCommands.filter((task) => task.background ?? isLiveCommand(task)) : base.commands,
          jobs: nextJobs ?? base.jobs,
        };
      });
      if (runActive || live) timer = setTimeout(load, POLL_MS);
    };
    void load();
    return () => { active = false; clearTimeout(timer); };
  }, [chatId, runActive, live, refresh]);

  const cancelCommand = useCallback(async (taskId: string) => {
    await cancelCommandApi(taskId, chatId || undefined);
    setRefresh((n) => n + 1);
  }, [chatId]);
  const cancelJob = useCallback(async (jobId: string) => {
    await cancelJobApi(jobId);
    setRefresh((n) => n + 1);
  }, []);

  return { commands, jobs, cancelCommand, cancelJob };
}
