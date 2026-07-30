import { useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import {
  CheckCircleFilled, CloseCircleFilled, LoadingOutlined, RightOutlined,
} from '@ant-design/icons';
import { CollapseHeight } from '../common/CollapseHeight';
import { DUR, EASE } from '../../utils/motionTokens';
import { useChatStore } from '../../stores';
import { t } from '../../i18n';

/* ───────────────────────────────────────────
   Plan bar — slim plan/progress strip pinned
   above the chat input. Fed by chatStore
   planProgress from the agent update_plan tool.
   Manual plan mode already renders its full progress
   inside the conversation. Collapsed: one 40px row
   with a determinate progress ring; click to
   expand the step checklist.
   ─────────────────────────────────────────── */

/** Determinate progress ring (HIG: prefer determinate when total is known). */
function ProgressRing({ completed, total }: { completed: number; total: number }) {
  const R = 7;
  const C = 2 * Math.PI * R;
  const frac = total > 0 ? Math.min(1, completed / total) : 0;
  return (
    <svg className="jx-planStrip-ring" width="18" height="18" viewBox="0 0 18 18" aria-hidden>
      <circle cx="9" cy="9" r={R} fill="none" stroke="var(--color-primary-bg)" strokeWidth="2.5" />
      <circle
        cx="9" cy="9" r={R} fill="none"
        stroke="var(--color-primary)" strokeWidth="2.5" strokeLinecap="round"
        strokeDasharray={C}
        strokeDashoffset={C * (1 - frac)}
        transform="rotate(-90 9 9)"
        style={{ transition: 'stroke-dashoffset .35s ease' }}
      />
    </svg>
  );
}

function StepIcon({ status }: { status: 'pending' | 'in_progress' | 'completed' | 'failed' }) {
  switch (status) {
    case 'completed':
      return <CheckCircleFilled className="jx-planStrip-stepIcon jx-planStrip-stepIcon--done" />;
    case 'failed':
      return <CloseCircleFilled className="jx-planStrip-stepIcon jx-planStrip-stepIcon--fail" />;
    case 'in_progress':
      return <LoadingOutlined spin className="jx-planStrip-stepIcon jx-planStrip-stepIcon--running" />;
    default:
      return <span className="jx-planStrip-stepIcon jx-planStrip-stepIcon--pending" />;
  }
}

export function PlanProgressStrip({ chatId }: { chatId: string }) {
  const progress = useChatStore((s) => s.planProgress[chatId]);
  const [expanded, setExpanded] = useState(false);

  // Manual plan mode already owns a full plan card in the message stream.
  // Showing the same steps here would duplicate that UI above the composer;
  // keep this compact strip for model-initiated update_plan progress only.
  const visibleProgress = progress?.source === 'agent' ? progress : null;

  const steps = visibleProgress?.steps ?? [];
  const completed = steps.filter((s) => s.status === 'completed').length;
  const failed = steps.filter((s) => s.status === 'failed').length;
  const total = steps.length;
  const current = steps.find((s) => s.status === 'in_progress');
  const allDone = !!visibleProgress?.done || (total > 0 && completed + failed === total);

  return (
    <AnimatePresence initial={false}>
      {visibleProgress && total > 0 && (
        <motion.div
          key="planStrip"
          className="jx-planStrip"
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 6, transition: { duration: DUR.fast, ease: EASE.exit } }}
          transition={{ duration: DUR.normal, ease: EASE.brandOut }}
        >
          <button
            type="button"
            className="jx-planStrip-header"
            onClick={() => setExpanded(!expanded)}
            aria-expanded={expanded}
            aria-label={t('任务计划：{done}/{total} 步已完成，点击{action}', {
              done: completed, total, action: expanded ? t('收起') : t('展开'),
            })}
          >
            {allDone
              ? (failed > 0
                ? <CloseCircleFilled className="jx-planStrip-doneIcon jx-planStrip-doneIcon--fail" />
                : <CheckCircleFilled className="jx-planStrip-doneIcon" />)
              : <ProgressRing completed={completed} total={total} />}
            <span className="jx-planStrip-title">{visibleProgress.title || t('任务计划')}</span>
            {!allDone && current && (
              <>
                <span className="jx-planStrip-sep" aria-hidden>·</span>
                <span className="jx-planStrip-current">{current.title}</span>
              </>
            )}
            {allDone && failed > 0 && (
              <>
                <span className="jx-planStrip-sep" aria-hidden>·</span>
                <span className="jx-planStrip-current">{t('{n} 步失败', { n: failed })}</span>
              </>
            )}
            <span className="jx-planStrip-count">{completed}/{total}</span>
            <motion.span
              className="jx-planStrip-chevron"
              initial={false}
              animate={{ rotate: expanded ? -90 : 90 }}
              transition={{ duration: DUR.fast, ease: EASE.standard }}
            >
              <RightOutlined />
            </motion.span>
          </button>

          <CollapseHeight show={expanded} duration={0.2}>
            <ul className="jx-planStrip-steps">
              {steps.map((s, i) => (
                <li
                  key={i}
                  className={`jx-planStrip-step jx-planStrip-step--${s.status}`}
                >
                  <StepIcon status={s.status} />
                  <span className="jx-planStrip-stepTitle">{s.title}</span>
                </li>
              ))}
            </ul>
          </CollapseHeight>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
