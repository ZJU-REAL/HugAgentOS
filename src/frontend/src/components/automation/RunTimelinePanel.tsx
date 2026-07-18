import { useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Tooltip } from 'antd';
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloseOutlined,
  MessageOutlined,
  LoadingOutlined,
  RightOutlined,
} from '@ant-design/icons';
import { useAutomationChatStore, useAutomationStore, useCatalogStore } from '../../stores';
import type { AutomationChatGroup, AutomationRun, AutomationRunStatus } from '../../types';
import { DUR, EASE, staggerStyle } from '../../utils/motionTokens';
import { useStatusFlash } from '../../hooks/useFlash';
import { pad2, formatMonthDay, formatMonthDayTime, formatFullDateTime, formatDateKey } from '../../utils/date';
import { RUN_STATUS_LABEL } from '../lab/automationUtils';
import '../../styles/automation-timeline.css';
import { t } from '../../i18n';

/* Stat number y6→0 crossfade (shared config between top stats and mini stats) */
const STAT_NUM_MOTION = {
  initial: { opacity: 0, y: 6 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -6 },
  transition: { duration: DUR.fast, ease: EASE.standard },
};

interface DateGroup {
  date: string;
  fullDate: string;
  runs: (AutomationRun & { runNo: number })[];
}

function formatDuration(durationMs?: number): string {
  if (!durationMs || durationMs <= 0) return '-';
  if (durationMs < 1000) return `${durationMs}ms`;
  const seconds = durationMs / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds >= 10 ? 0 : 1)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainSeconds = Math.round(seconds % 60);
  return `${minutes}m ${pad2(remainSeconds)}s`;
}

// First, second … tenth; above 10 use Arabic numerals
const CN_DIGITS = ['', '一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];
function formatRunOrdinal(n: number): string {
  return `第${CN_DIGITS[n] || n}次`;
}

function summarizeRun(run: AutomationRun): string {
  if (run.status === 'running') return t('任务正在执行，结果生成后会自动更新到对话区。');
  if (run.status === 'failed') {
    return String(run.error_message || run.result_summary || t('执行失败，请进入详情查看错误信息。')).replace(/\s+/g, ' ').trim();
  }
  return String(run.result_summary || t('执行完成，可进入对话查看完整输出与附件结果。')).replace(/\s+/g, ' ').trim();
}

export function RunTimelinePanel() {
  const { activeGroup, selectedRunId, selectRun, exitAutomationChat } = useAutomationChatStore();
  const { setSelectedTaskId } = useAutomationStore();
  const { setPanel } = useCatalogStore();

  // Exit snapshot: when App.tsx's AnimatePresence plays the exit animation, store.activeGroup is already null,
  // so during render we derive state that caches the last non-empty group to render the final frame as a fallback
  // (the React-endorsed derived-state pattern), avoiding a blank panel during exit.
  const [snapshot, setSnapshot] = useState(activeGroup);
  if (activeGroup && activeGroup !== snapshot) setSnapshot(activeGroup);
  const group: AutomationChatGroup | null = activeGroup ?? snapshot;

  const numberedRuns = useMemo<(AutomationRun & { runNo: number })[]>(() => {
    if (!group) return [];

    // Number runs in ascending order (oldest = #1)
    const allRuns = [...group.runs];
    const ascending = [...allRuns].reverse();
    return allRuns.map((run) => ({
      ...run,
      runNo: ascending.findIndex((r) => r.run_id === run.run_id) + 1,
    }));
  }, [group]);

  // run_id → flat index (used for enter stagger, following render order consistent with groupedRuns)
  const runIndexMap = useMemo(() => {
    const map = new Map<string, number>();
    numberedRuns.forEach((run, idx) => map.set(run.run_id, idx));
    return map;
  }, [numberedRuns]);

  // The running → finished flip frame gives the card a one-time border flash (auto-cleared after 800ms).
  const justFinished = useStatusFlash(
    numberedRuns,
    (run) => run.run_id,
    (run) => run.status,
    (prev) => prev === 'running',
    800,
  );

  const groupedRuns = useMemo<DateGroup[]>(() => {
    if (!group) return [];

    // Group by date (runs are already desc by started_at)
    const map = new Map<string, DateGroup>();
    for (const run of numberedRuns) {
      const key = formatDateKey(run.started_at);
      if (!map.has(key)) {
        map.set(key, {
          date: formatMonthDay(run.started_at),
          fullDate: key,
          runs: [],
        });
      }
      map.get(key)!.runs.push(run);
    }
    return Array.from(map.values());
  }, [group, numberedRuns]);

  // Accumulate all status counts + the most recent success record in a single pass, avoiding the O(kn) cost of multiple filter/find calls.
  const stats = useMemo(() => {
    let running = 0;
    let success = 0;
    let failed = 0;
    let latestSuccessRun: AutomationRun | undefined;
    let latestFinishedRun: AutomationRun | undefined;
    for (const run of numberedRuns) {
      if (run.status === 'running') running += 1;
      else if (run.status === 'success') {
        success += 1;
        if (!latestSuccessRun) latestSuccessRun = run;
        if (!latestFinishedRun) latestFinishedRun = run;
      } else if (run.status === 'failed') {
        failed += 1;
        if (!latestFinishedRun) latestFinishedRun = run;
      }
    }
    const total = numberedRuns.length;
    const completed = total - running;
    const successRate = completed > 0 ? Math.round((success / completed) * 1000) / 10 : 0;
    return { total, running, success, failed, completed, successRate, latestSuccessRun, latestFinishedRun };
  }, [numberedRuns]);

  if (!group) return null;

  const { total: totalCount, running: runningCount, success: successCount, failed: failedCount,
    completed: completedCount, successRate, latestSuccessRun, latestFinishedRun } = stats;
  const latestRun = numberedRuns[0];

  const navigateBackToDetail = () => {
    setSelectedTaskId(group.taskId);
    setPanel('app_center');
    exitAutomationChat();
  };

  const latestLabel = latestRun ? t('最近一次{status}', { status: RUN_STATUS_LABEL[latestRun.status] }) : t('暂无执行');
  const latestIndicatorRun = latestSuccessRun || latestRun;
  const latestIndicatorStatus: AutomationRunStatus | 'idle' =
    latestIndicatorRun ? latestIndicatorRun.status : 'idle';

  const topStats: { label: string; value: string | number; primary?: boolean }[] = [
    { label: t('执行次数'), value: totalCount, primary: true },
    { label: t('成功率'), value: `${successRate}%`, primary: true },
    { label: t('最近完成时间'), value: latestFinishedRun ? formatMonthDay(latestFinishedRun.started_at) : '—' },
  ];
  const miniStats: { label: string; value: number }[] = [
    { label: t('全部'), value: totalCount },
    { label: t('成功'), value: successCount },
    { label: t('失败'), value: failedCount },
    { label: t('执行中'), value: runningCount },
  ];

  return (
    <div className="jx-runTimeline">
      {/* Top bar: Automation task >   X */}
      <div className="jx-runTimeline-topBar">
        <button
          type="button"
          className="jx-runTimeline-crumb"
          onClick={navigateBackToDetail}
          title={t('返回自动化任务详情')}
        >
          <span>{t('自动化任务')}</span>
          <RightOutlined className="jx-runTimeline-crumbIcon" />
        </button>
        <button
          type="button"
          className="jx-runTimeline-closeBtn"
          onClick={exitAutomationChat}
          aria-label={t('关闭')}
          title={t('关闭面板')}
        >
          <CloseOutlined />
        </button>
      </div>

      {/* Scrollable body */}
      <div className="jx-runTimeline-body">
        {/* Title + subtitle */}
        <div className="jx-runTimeline-titleBlock">
          <div className="jx-runTimeline-title" title={group.taskName}>
            {group.taskName}
          </div>
          <div className="jx-runTimeline-subtitle">
            {t('运行舱记录最近执行状态、摘要与异常线索')}
          </div>
        </div>

        {/* Top 3-column stats */}
        <div className="jx-runTimeline-statRow">
          {topStats.map((s) => (
            <div key={s.label} className="jx-runTimeline-statCard">
              <span className="jx-runTimeline-statLabel">{s.label}</span>
              <strong className={`jx-runTimeline-statValue${s.primary ? ' is-primary' : ''}`}>
                {/* Number-change y6→0 crossfade: key bound to the value itself, so polling with the same value re-renders without replaying */}
                <AnimatePresence mode="popLayout" initial={false}>
                  <motion.span key={String(s.value)} {...STAT_NUM_MOTION}>
                    {s.value}
                  </motion.span>
                </AnimatePresence>
              </strong>
            </div>
          ))}
        </div>

        {/* Most recent status */}
        <div className={`jx-runTimeline-latestRow is-${latestIndicatorStatus}`}>
          <span className="jx-runTimeline-latestIcon">
            {latestIndicatorStatus === 'success' && <CheckCircleOutlined />}
            {latestIndicatorStatus === 'failed' && <CloseCircleOutlined />}
            {latestIndicatorStatus === 'running' && <LoadingOutlined spin />}
            {latestIndicatorStatus === 'idle' && <CheckCircleOutlined />}
          </span>
          <span className="jx-runTimeline-latestLabel">
            {latestIndicatorRun ? latestLabel : t('暂无执行')}：
          </span>
          <span className="jx-runTimeline-latestTime">
            {latestIndicatorRun ? formatFullDateTime(latestIndicatorRun.started_at) : '—'}
          </span>
        </div>

        {/* Execution records */}
        <section className="jx-runTimeline-section">
          <div className="jx-runTimeline-sectionHeading">
            <span className="jx-runTimeline-sectionBar" />
            <span>{t('执行记录')}</span>
          </div>
          <div className="jx-runTimeline-sectionMeta">
            {t('已完成 {done} 次，失败 {failed} 次', { done: completedCount, failed: failedCount })}
          </div>
          <div className="jx-runTimeline-miniStatRow">
            {miniStats.map((s) => (
              <div key={s.label} className="jx-runTimeline-miniStat">
                <span className="jx-runTimeline-miniStatLabel">{s.label}</span>
                <strong className="jx-runTimeline-miniStatValue">
                  <AnimatePresence mode="popLayout" initial={false}>
                    <motion.span key={String(s.value)} {...STAT_NUM_MOTION}>
                      {s.value}
                    </motion.span>
                  </AnimatePresence>
                </strong>
              </div>
            ))}
          </div>
        </section>

        {/* Detail timeline */}
        <section className="jx-runTimeline-section">
          <div className="jx-runTimeline-sectionHeading">
            <span className="jx-runTimeline-sectionBar" />
            <span>{t('详情')}</span>
          </div>

          {groupedRuns.length === 0 ? (
            <div className="jx-runTimeline-empty">
              <div className="jx-runTimeline-emptyTitle">{t('暂无运行记录')}</div>
              <div className="jx-runTimeline-emptyHint">{t('等待下一次自动执行或手动触发任务')}</div>
            </div>
          ) : (
            <div className="jx-runTimeline-timeline">
              {groupedRuns.map((group) => (
                <div key={group.fullDate} className="jx-runTimeline-dateGroup">
                  <div className="jx-runTimeline-dateHeader">
                    <span className="jx-runTimeline-dateLabel">{group.date}</span>
                    <span className="jx-runTimeline-dateCount">{t('{n} 次', { n: group.runs.length })}</span>
                  </div>
                  <div className="jx-runTimeline-dateBody">
                    {group.runs.map((run) => {
                      const isActive = run.run_id === selectedRunId;
                      const isRunning = run.status === 'running';
                      const isClickable = !isRunning && !!run.chat_id;
                      const summary = summarizeRun(run);

                      return (
                        <Tooltip
                          key={run.run_id}
                          title={isRunning ? t('执行中，暂不可查看') : RUN_STATUS_LABEL[run.status]}
                          placement="left"
                        >
                          <div
                            className={[
                              'jx-runTimeline-item',
                              isActive && 'is-active',
                              `is-${run.status}`,
                              !isClickable && 'is-disabled',
                              justFinished.has(run.run_id) && 'is-justFinished',
                            ].filter(Boolean).join(' ')}
                            style={staggerStyle(runIndexMap.get(run.run_id) ?? 0)}
                            role={isClickable ? 'button' : undefined}
                            tabIndex={isClickable ? 0 : -1}
                            onClick={() => isClickable && selectRun(run.run_id)}
                            onKeyDown={(event) => {
                              if (!isClickable) return;
                              if (event.key === 'Enter' || event.key === ' ') {
                                event.preventDefault();
                                selectRun(run.run_id);
                              }
                            }}
                          >
                            <div className="jx-runTimeline-itemTop">
                              <div className="jx-runTimeline-itemIdentity">
                                <span className="jx-runTimeline-runNo">{formatRunOrdinal(run.runNo)}</span>
                                <span className={`jx-runTimeline-statusTag is-${run.status}${isRunning ? ' jx-anim-ripple' : ''}`}>
                                  {RUN_STATUS_LABEL[run.status]}
                                </span>
                              </div>
                              <span className="jx-runTimeline-itemTime">
                                {formatMonthDayTime(run.started_at)}
                              </span>
                            </div>
                            <div className="jx-runTimeline-itemSummary">{summary}</div>
                            <div className="jx-runTimeline-itemBottom">
                              <span className="jx-runTimeline-itemDuration">
                                {run.duration_ms
                                  ? t('耗时 {dur}', { dur: formatDuration(run.duration_ms) })
                                  : (isRunning ? t('执行中') : t('耗时 —'))}
                              </span>
                              <span
                                className={[
                                  'jx-runTimeline-itemEnter',
                                  !isClickable && 'is-disabled',
                                ].filter(Boolean).join(' ')}
                              >
                                <MessageOutlined />
                                <span>{isClickable ? t('进入对话') : t('生成中')}</span>
                              </span>
                            </div>
                          </div>
                        </Tooltip>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
