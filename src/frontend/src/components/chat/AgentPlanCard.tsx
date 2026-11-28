import { memo, type SVGProps } from 'react';
import { usePlanView } from './usePlanView';
import type { PlanProgressState } from '../../types';
import { t } from '../../i18n';
// The running step uses the shared neutral task spinner.
import '../../styles/task-status.css';

/* ───────────────────────────────────────────
   Agent plan card — the update_plan checklist,
   shown inside the latest reply just above its
   tool calls. Fed by chatStore planProgress.
   Manual plan mode renders its own PlanCard in
   the message stream instead.
   A plain list of steps, always open, in neutral
   tones: the current step carries the only motion.
   ─────────────────────────────────────────── */

type StepStatus = PlanProgressState['steps'][number]['status'] | 'stopped';

/** One outline ring per status, plus the mark drawn inside it. */
const GLYPHS: Record<Exclude<StepStatus, 'in_progress'>, { ring?: SVGProps<SVGCircleElement>; mark?: string }> = {
  completed: { mark: 'M5.25 8.25L7.1 10L10.75 6.25' },
  failed: { mark: 'M6 6L10 10M10 6L6 10' },
  stopped: { mark: 'M6.5 5.75V10.25M9.5 5.75V10.25' },
  pending: { ring: { strokeDasharray: '2.5 2.25' } },
};

/** 16px glyphs in currentColor: status reads from the shape, not from colour. */
function StepGlyph({ status }: { status: StepStatus }) {
  if (status === 'in_progress') {
    return (
      <svg className="jx-agentPlan-glyph" width="16" height="16" viewBox="0 0 24 24" aria-hidden>
        <g className="jx-taskSpinnerMotion">
          <circle className="jx-taskSpinnerTrack" cx="12" cy="12" r="9.5" />
          <circle className="jx-taskSpinnerArc" cx="12" cy="12" r="9.5" />
        </g>
      </svg>
    );
  }
  const { ring, mark } = GLYPHS[status];
  return (
    <svg
      className="jx-agentPlan-glyph" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden
      stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"
    >
      <circle cx="8" cy="8" r="6.75" {...ring} />
      {mark && <path d={mark} />}
    </svg>
  );
}

/** Memoized: the reply it sits in re-renders on every streamed token, the plan does not. */
export const AgentPlanCard = memo(function AgentPlanCard({ chatId }: { chatId: string }) {
  const view = usePlanView(chatId);
  if (!view) return null;
  const { title, steps, completed, total, stopped } = view;
  return (
    <ol
      className="jx-agentPlan"
      aria-label={t('{title}：{done}/{total} 步已完成', { title: title || t('任务计划'), done: completed, total })}
    >
      {steps.map((s, i) => {
        const status: StepStatus = stopped && s.status === 'in_progress' ? 'stopped' : s.status;
        return (
          <li key={i} className="jx-agentPlan-step" data-status={status}>
            <span className="jx-agentPlan-lead"><StepGlyph status={status} /></span>
            <span className="jx-agentPlan-stepTitle">{s.title}</span>
          </li>
        );
      })}
    </ol>
  );
});
