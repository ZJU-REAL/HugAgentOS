import { useState } from 'react';
import { t } from '../../i18n';
import { useChatStore } from '../../stores';
import { useLoopStore } from '../../stores/loopStore';
import './loop.css';

const STATUS_LABEL: Record<string, string> = {
  running: '进行中',
  completed: '已达成',
  budget_exhausted: '预算用尽',
  cancelled: '已取消',
  awaiting_human: '待人工',
  failed: '失败',
};

interface LoopPlanBarProps {
  /** "Continue" a stopped/disconnected and unfinished loop (resume the same loop_id from its breakpoint). */
  onContinue?: (chatId?: string) => void;
}

/**
 * The "plan bar" for the in-chat autonomous loop -- shown above the input box: current goal +
 * requirement checklist + which step it's on (✓ done / ▶ in progress / ○ to-do). Replaces the old "round N" table.
 */
export default function LoopPlanBar({ onContinue }: LoopPlanBarProps) {
  const currentChatId = useChatStore((s) => s.currentChatId);
  const livePlan = useLoopStore((s) => s.livePlan);
  const clearLivePlan = useLoopStore((s) => s.clearLivePlan);
  const [collapsed, setCollapsed] = useState(false);

  if (!livePlan || livePlan.chatId !== currentChatId) return null;

  const { objective, requirements, currentId, progress, status, loopId, reviewing } = livePlan;
  const running = status === 'running' || !status;
  const statusLabel = STATUS_LABEL[status || 'running'] || status;
  const done = requirements.filter((r) => r.passes).length;
  const total = requirements.length;
  const pct = total > 0 ? Math.round((done / total) * 100) : (running ? 8 : 100);
  // Stopped/disconnected and unfinished (not running, not completed, not all requirements passed) -> can resume from breakpoint.
  const canContinue = !running && status !== 'completed' && !!loopId && !!onContinue
    && !(total > 0 && done >= total);

  return (
    <div className={`loop-planbar${running ? ' loop-planbar-running' : ''}`}>
      <div className="loop-planbar-head" onClick={() => setCollapsed((v) => !v)}>
        <span className="loop-planbar-icon">🔁</span>
        <span className="loop-planbar-title" title={objective}>{objective}</span>
        <span className={`loop-planbar-status loop-status-${status || 'running'}`}>
          {statusLabel}{progress ? ` · ${progress}` : ''}
        </span>
        <span className="loop-planbar-caret">{collapsed ? '▸' : '▾'}</span>
        {canContinue && (
          <button
            className="loop-planbar-continue"
            title={t('从断点继续这个循环')}
            onClick={(e) => { e.stopPropagation(); onContinue?.(currentChatId ?? undefined); }}
          >▶ {t('继续')}</button>
        )}
        {!running && (
          <button
            className="loop-planbar-close"
            title={t('关闭')}
            onClick={(e) => { e.stopPropagation(); clearLivePlan(currentChatId ?? undefined); }}
          >×</button>
        )}
      </div>
      <div className="loop-planbar-progress">
        <div className="loop-planbar-progress-fill" style={{ width: `${pct}%` }} />
      </div>
      {!collapsed && total > 0 && (
        <ul className="loop-planbar-list">
          {requirements.map((r) => {
            const state = r.passes ? 'done' : (r.id === currentId ? 'active' : 'todo');
            const mark = state === 'done' ? '✓' : (state === 'active' ? '▶' : '○');
            return (
              <li key={r.id} className={`loop-planbar-item loop-item-${state}`}>
                <span className="loop-planbar-mark">{mark}</span>
                <span className="loop-planbar-desc">{r.description}</span>
                {state === 'active' && reviewing && running && (
                  <span className="loop-planbar-reviewing" title={t('只读评审子智能体正在核验真实产出')}>
                    🔍 {t('评审中')}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {!collapsed && total === 0 && running && (
        <div className="loop-planbar-hint">{t('正在拆解目标为需求清单…')}</div>
      )}
    </div>
  );
}
