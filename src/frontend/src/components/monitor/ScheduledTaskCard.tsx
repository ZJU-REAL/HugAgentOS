import { useEffect, useMemo, useState } from 'react';
import { RightOutlined } from '@ant-design/icons';
import { getAutomation, getAutomationRuns } from '../../api';
import { t } from '../../i18n';
import { useAutomationChatStore, useCatalogStore } from '../../stores';
import type { AutomationRun } from '../../types';
import { formatMonthDayTime } from '../../utils/date';
import { RUN_STATUS_LABEL, formatRunOrdinal, isRunActive, summarizeRun } from '../automation/automationUtils';
import { MonitorCard } from './MonitorCard';

/* Refresh while a run is active: status flips and chat_id links reach the card through it. */
const RUN_POLL_INTERVAL_MS = 5_000;

/**
 * The scheduled task this run conversation belongs to: every run, newest first,
 * each one opening its own conversation. Runs come from the active automation
 * group when the route set one, otherwise they are fetched here.
 */
export function ScheduledTaskCard({ taskId, chatId }: { taskId: string; chatId: string }) {
  const activeGroup = useAutomationChatStore((s) => s.activeGroup);
  const setRuns = useAutomationChatStore((s) => s.setRuns);
  const group = activeGroup?.taskId === taskId ? activeGroup : null;
  const [fallback, setFallback] = useState<{ taskName: string; runs: AutomationRun[] } | null>(null);
  const taskName = group?.taskName ?? fallback?.taskName ?? '';
  const runs = useMemo(() => group?.runs ?? fallback?.runs ?? [], [group, fallback]);

  useEffect(() => {
    if (group || taskId === 'new') return;
    let cancelled = false;
    void Promise.all([getAutomation(taskId), getAutomationRuns(taskId, 50)])
      .then(([task, list]) => { if (!cancelled) setFallback({ taskName: task.name || taskId, runs: list }); })
      .catch(() => { /* The card stays hidden; the conversation itself still works. */ });
    return () => { cancelled = true; };
  }, [group, taskId]);

  const polling = runs.some(isRunActive);
  useEffect(() => {
    if (!polling) return;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      if (document.hidden) return;
      try {
        const list = await getAutomationRuns(taskId, 50);
        if (cancelled) return;
        // A response that lands after the user moved to another task must not write back.
        if (useAutomationChatStore.getState().activeGroup?.taskId === taskId) setRuns(list);
        else setFallback((prev) => (prev ? { ...prev, runs: list } : prev));
      } catch { /* Next tick retries. */ }
    }, RUN_POLL_INTERVAL_MS);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [polling, taskId, setRuns]);

  if (taskId === 'new' || (!group && !fallback)) return null;

  const running = runs.filter(isRunActive).length;
  const completed = runs.length - running;
  const success = runs.filter((run) => run.status === 'success').length;
  const rate = completed > 0 ? Math.round((success / completed) * 100) : 0;

  const openDetail = () => {
    // The task id goes along with the jump, otherwise the panel falls back to the task list.
    useCatalogStore.getState().setPanel('automation', taskId);
    useAutomationChatStore.getState().exitAutomationChat();
  };
  const openRun = (run: AutomationRun) => {
    const store = useAutomationChatStore.getState();
    if (store.activeGroup?.taskId === taskId) store.selectRun(run.run_id);
    else store.enterAutomationChat(taskId, taskName, runs, run.run_id);
  };

  return (
    <MonitorCard
      title={t('定时任务')}
      className="jx-runMonitor-schedule"
      summary={running > 0
        ? <span className="jx-runMonitor-scheduleLive">{t('{n} 运行中', { n: running })}</span>
        : completed > 0 && <span className="jx-runMonitor-num">{t('{n} 次', { n: runs.length })} · {rate}%</span>}
    >
      <button type="button" className="jx-runMonitor-row jx-runMonitor-scheduleTask" title={t('返回定时任务详情')} onClick={openDetail}>
        <span className="jx-runMonitor-name">{taskName}</span>
        <RightOutlined className="jx-runMonitor-glyph" aria-hidden />
      </button>
      {runs.length === 0
        ? <div className="jx-runMonitor-scheduleEmpty">{t('暂无运行记录')}</div>
        : <div className="jx-runMonitor-scheduleRuns">
          {runs.map((run, index) => {
            const summary = summarizeRun(run);
            return (
              <button
                key={run.run_id}
                type="button"
                className="jx-runMonitor-row"
                data-active={run.chat_id === chatId || undefined}
                disabled={!run.chat_id}
                title={run.chat_id ? summary : t('准备中，暂不可查看')}
                onClick={() => openRun(run)}
              >
                <span className="jx-runMonitor-lead">
                  <span className={`jx-runMonitor-runDot${run.status === 'running' ? ' jx-anim-ripple' : ''}`} data-status={run.status} aria-label={RUN_STATUS_LABEL[run.status]} role="img" />
                </span>
                <span className="jx-runMonitor-name">
                  {formatRunOrdinal(runs.length - index)}
                  <span className="jx-runMonitor-runSummary"> {summary}</span>
                </span>
                <span className="jx-runMonitor-meta">{formatMonthDayTime(run.started_at)}</span>
              </button>
            );
          })}
        </div>}
    </MonitorCard>
  );
}
