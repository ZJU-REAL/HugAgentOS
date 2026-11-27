import { useChatStore } from '../../stores';
import type { PlanProgressState } from '../../types';

/**
 * Derived view of an agent plan. A plan only moves while its chat has a run
 * going: stopping a run (or a run that dies without a closing event) leaves
 * `done` unset, so "settled" comes from the run state here instead of trusting
 * every stop path to mark the plan. Workflow chats hand work to a background
 * job that updates the plan in a later turn, so theirs stays open until the
 * server settles it.
 */
function derivePlanView(
  progress: PlanProgressState | null | undefined,
  { running, workflowChat }: { running: boolean; workflowChat: boolean },
) {
  // Manual plan mode already owns a full plan card in the message stream;
  // these views are for model-initiated update_plan progress only.
  if (progress?.source !== 'agent') return null;
  const steps = progress.steps;
  const total = steps.length;
  if (total === 0) return null;
  const completed = steps.filter((s) => s.status === 'completed').length;
  const finished = steps.every((s) => s.status === 'completed' || s.status === 'failed');
  const settled = finished || !!progress.done || (!running && !workflowChat);
  // Settled before every step finished: the run was stopped or ended early.
  const stopped = settled && !finished;
  return { title: progress.title, steps, completed, total, stopped };
}

/** Agent-reported plan for a chat, with the derived counts the plan card needs. */
export function usePlanView(chatId: string | null | undefined) {
  const progress = useChatStore((s) => (chatId ? s.planProgress[chatId] : undefined));
  const running = useChatStore((s) => !!chatId && (
    s.sendingChatIds.has(chatId) || !!s.activeRuns[chatId] || s.remoteRunningChatIds.has(chatId)
  ));
  const workflowChat = useChatStore((s) => !!chatId && s.store.chats[chatId]?.workflowChat === true);
  return derivePlanView(progress, { running, workflowChat });
}
