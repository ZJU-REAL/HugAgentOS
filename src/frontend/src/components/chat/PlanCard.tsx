import { useEffect, useRef, useState, type ReactNode } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import {
  CheckCircleFilled, CloseCircleFilled, LoadingOutlined,
  RightOutlined,
  ToolOutlined, AppstoreOutlined, SafetyCertificateOutlined,
  ThunderboltOutlined, OrderedListOutlined, RobotOutlined,
} from '@ant-design/icons';
import { DUR, EASE, SPRING, staggerStyle } from '../../utils/motionTokens';
import { CollapseHeight } from '../common/CollapseHeight';
import { t } from '../../i18n';

/* ───────────────────────────────────────────
   Plan Card — structured plan rendering
   ─────────────────────────────────────────── */

export interface PlanStepData {
  step_order: number;
  title: string;
  description?: string;
  expected_tools?: string[];
  expected_skills?: string[];
  expected_agents?: string[];
  acceptance_criteria?: string;
  status?: 'pending' | 'running' | 'success' | 'failed' | 'skipped';
  summary?: string;
  text?: string;          // live progress text during execution
}

export interface PlanCardProps {
  mode: 'preview' | 'executing' | 'complete';
  title: string;
  description?: string;
  steps: PlanStepData[];
  completedSteps?: number;
  totalSteps?: number;
  resultText?: string;    // final report for 'complete' mode
  isStreaming?: boolean;
  agentNameMap?: Record<string, string>;
  /**
   * Override the footer rendered in 'preview' mode.
   * - `undefined` → default "please reply to confirm execution" prompt (chat scenario)
   * - `null`       → hide the footer
   * - ReactNode    → custom footer content
   */
  previewFooter?: ReactNode | null;
  /** Extra className, for variant styles (such as embed) */
  className?: string;
  /** Expand steps by default (in preview mode) */
  defaultExpandSteps?: boolean;
}

/* ── StepStatusIcon transition animation config (module-level constants, not rebuilt on high-frequency SSE re-renders) ── */
const STATUS_ICON_INITIAL = { scale: 0.8, opacity: 0 };
const STATUS_ICON_INITIAL_SUCCESS = { scale: 0.4, opacity: 0 };
const STATUS_ICON_ANIMATE = { scale: 1, opacity: 1 };
const STATUS_ICON_EXIT = { scale: 0.8, opacity: 0, transition: { duration: 0.1, ease: EASE.exit } };
const STATUS_ICON_SWAP = { duration: DUR.fast, ease: EASE.standard };

/* ── Status icon helper ── */
function StepStatusIcon({ status }: { status?: string }) {
  const key = status || 'pending';
  let icon: ReactNode;
  switch (status) {
    case 'success':
      icon = <CheckCircleFilled className="jx-plan-stepIcon jx-plan-stepIcon--success" />;
      break;
    case 'failed':
      icon = <CloseCircleFilled className="jx-plan-stepIcon jx-plan-stepIcon--error" />;
      break;
    case 'running':
      icon = <LoadingOutlined className="jx-plan-stepIcon jx-plan-stepIcon--running" spin />;
      break;
    case 'skipped':
      icon = <span className="jx-plan-stepIcon jx-plan-stepIcon--skipped">—</span>;
      break;
    default:
      icon = <span className="jx-plan-stepIcon jx-plan-stepIcon--pending" />;
  }
  // The animation key binds only to status: high-frequency SSE re-renders won't replay it; the transition only fires when the status flips.
  // initial={false} makes it not play on history load / first mount (avoiding the whole column of checks popping at once).
  // success uses a spring pop-in — the only deliberate bounce "reward moment" in the whole design.
  return (
    <AnimatePresence mode="wait" initial={false}>
      <motion.span
        key={key}
        className="jx-plan-stepIconWrap"
        initial={key === 'success' ? STATUS_ICON_INITIAL_SUCCESS : STATUS_ICON_INITIAL}
        animate={STATUS_ICON_ANIMATE}
        exit={STATUS_ICON_EXIT}
        transition={key === 'success' ? SPRING.pop : STATUS_ICON_SWAP}
      >
        {icon}
      </motion.span>
    </AnimatePresence>
  );
}

/* ── Single step row ── */
function PlanStepRow({ step, index, mode, agentNameMap, defaultExpanded }: { step: PlanStepData; index: number; mode: string; agentNameMap?: Record<string, string>; defaultExpanded?: boolean }) {
  const [expanded, setExpanded] = useState(!!defaultExpanded);
  const hasDetails = !!(step.description || step.expected_tools?.length || step.expected_skills?.length || step.expected_agents?.length || step.acceptance_criteria);
  const showExpand = mode === 'preview' && hasDetails;
  const isActive = step.status === 'running';
  const progressText = step.text ?? '';

  // Fire a one-shot background flash on the frame status actually flips to success;
  // the ref's initial value takes the first-frame status — rows already in success on history load don't flash.
  // setState is triggered asynchronously via setTimeout to avoid a synchronous setState cascade render inside the effect.
  const prevStatusRef = useRef(step.status);
  const [justDone, setJustDone] = useState(false);
  useEffect(() => {
    const prev = prevStatusRef.current;
    prevStatusRef.current = step.status;
    if (step.status === 'success' && prev && prev !== 'success') {
      const onTimer = window.setTimeout(() => setJustDone(true), 0);
      const offTimer = window.setTimeout(() => setJustDone(false), 600);
      return () => {
        window.clearTimeout(onTimer);
        window.clearTimeout(offTimer);
      };
    }
  }, [step.status]);

  return (
    // Entry stagger: the container .jx-plan-steps carries .jx-anim-stagger (40ms step),
    // row number starts at 1 (label occupies 0), capped at 6 → replicating the original nth-child 40~240ms delay ramp.
    <div
      className={`jx-plan-step ${isActive ? 'jx-plan-step--active' : ''} ${step.status === 'success' ? 'jx-plan-step--done' : ''} ${justDone ? 'jx-plan-step--justDone' : ''}`}
      style={staggerStyle(index + 1, 6)}
    >
      <div className="jx-plan-stepHeader" onClick={showExpand ? () => setExpanded(!expanded) : undefined} style={showExpand ? { cursor: 'pointer' } : undefined}>
        <div className="jx-plan-stepLeft">
          {mode === 'preview' ? (
            <span className="jx-plan-stepNum">{index + 1}</span>
          ) : (
            <StepStatusIcon status={step.status} />
          )}
          <span className="jx-plan-stepTitle">{step.title}</span>
        </div>
        {showExpand && (
          <motion.span
            className="jx-plan-stepExpand"
            initial={false}
            animate={{ rotate: expanded ? 90 : 0 }}
            transition={{ duration: DUR.fast, ease: EASE.standard }}
          >
            <RightOutlined />
          </motion.span>
        )}
      </div>

      {/* Preview mode: expandable details (height-auto expand; CollapseHeight defaults to initial=false so defaultExpandSteps initial expansion doesn't play) */}
      <CollapseHeight show={expanded && mode === 'preview'} duration={0.18}>
        <div className="jx-plan-stepDetails">
          {step.description && <p className="jx-plan-stepDesc">{step.description}</p>}
          {step.expected_tools && step.expected_tools.length > 0 && (
            <div className="jx-plan-stepMeta">
              <ToolOutlined className="jx-plan-metaIcon" />
              <span className="jx-plan-metaLabel">{t('MCP 工具')}</span>
              <div className="jx-plan-tags">
                {step.expected_tools.map((t, i) => <span key={i} className="jx-plan-tag jx-plan-tag--tool">{t}</span>)}
              </div>
            </div>
          )}
          {step.expected_skills && step.expected_skills.length > 0 && (
            <div className="jx-plan-stepMeta">
              <AppstoreOutlined className="jx-plan-metaIcon" />
              <span className="jx-plan-metaLabel">{t('技能')}</span>
              <div className="jx-plan-tags">
                {step.expected_skills.map((s, i) => <span key={i} className="jx-plan-tag jx-plan-tag--skill">{s}</span>)}
              </div>
            </div>
          )}
          {step.expected_agents && step.expected_agents.length > 0 && (
            <div className="jx-plan-stepMeta">
              <RobotOutlined className="jx-plan-metaIcon" />
              <span className="jx-plan-metaLabel">{t('子智能体')}</span>
              <div className="jx-plan-tags">
                {step.expected_agents.map((a, i) => <span key={i} className="jx-plan-tag jx-plan-tag--agent">{agentNameMap?.[a] || a}</span>)}
              </div>
            </div>
          )}
          {step.acceptance_criteria && (
            <div className="jx-plan-stepMeta">
              <SafetyCertificateOutlined className="jx-plan-metaIcon" />
              <span className="jx-plan-metaLabel">{t('验收标准')}</span>
              <span className="jx-plan-metaVal">{step.acceptance_criteria}</span>
            </div>
          )}
        </div>
      </CollapseHeight>

      {/* Execution mode: summary or live progress */}
      {mode !== 'preview' && step.summary && (
        <div className="jx-plan-stepSummary">{step.summary}</div>
      )}
      {/* The progress block appears/disappears via height-auto; the inner streaming text isn't animated (outer overflow hidden) */}
      {mode !== 'preview' && (
        <CollapseHeight show={!!(isActive && step.text)} duration={0.22}>
          <div className="jx-plan-stepProgress">
            {progressText.length > 300 ? progressText.slice(-300) : progressText}
          </div>
        </CollapseHeight>
      )}
    </div>
  );
}

/* ── Progress bar ── */
function ProgressBar({ completed, total }: { completed: number; total: number }) {
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
  return (
    <div className="jx-plan-progress">
      <div className="jx-plan-progressTrack">
        <div className="jx-plan-progressFill" style={{ width: `${pct}%` }} />
      </div>
      <span className="jx-plan-progressLabel">{completed}/{total}</span>
    </div>
  );
}

/* ── Main PlanCard ── */
export function PlanCard({ mode, title, description, steps, completedSteps, totalSteps, resultText, isStreaming, agentNameMap, previewFooter, className, defaultExpandSteps }: PlanCardProps) {
  const [stepsCollapsed, setStepsCollapsed] = useState(false);
  const completed = completedSteps ?? steps.filter(s => s.status === 'success').length;
  const total = totalSteps ?? steps.length;
  const isComplete = mode === 'complete';
  const isExecuting = mode === 'executing';
  // Complete on first mount (history load) doesn't play the completion celebration; only executing→complete within this session plays it.
  // The useState lazy initial value is taken only once on first mount, equivalent to "was already complete at mount" (lint forbids reading a ref during render).
  const [wasCompleteOnMount] = useState(isComplete);
  const justCompleted = isComplete && !wasCompleteOnMount;
  // In complete mode with resultText, default to collapsed steps
  const showSteps = isComplete && resultText ? !stepsCollapsed : true;

  return (
    <div className={`jx-plan-card ${isComplete ? 'jx-plan-card--complete' : ''} ${isExecuting ? 'jx-plan-card--executing' : ''} ${justCompleted ? 'jx-plan-card--justCompleted' : ''} ${className ?? ''}`}>
      {/* Header */}
      <div className="jx-plan-header">
        <div className="jx-plan-headerIcon">
          {isComplete ? (
            <CheckCircleFilled style={{ fontSize: 18, color: 'var(--color-success)' }} />
          ) : isExecuting ? (
            <ThunderboltOutlined style={{ fontSize: 18, color: 'var(--color-primary)' }} />
          ) : (
            <OrderedListOutlined style={{ fontSize: 18, color: 'var(--color-primary)' }} />
          )}
        </div>
        <div className="jx-plan-headerText">
          <h3 className="jx-plan-title">{title}</h3>
          {description && <p className="jx-plan-desc">{description}</p>}
        </div>
        {(isExecuting || isComplete) && (
          <div className="jx-plan-headerBadge">
            <span className={`jx-plan-badge ${isComplete ? 'jx-plan-badge--done' : 'jx-plan-badge--running'}`}>
              {isComplete ? t('已完成') : t('执行中')}
            </span>
          </div>
        )}
      </div>

      {/* Progress bar (execution & complete) */}
      {(isExecuting || isComplete) && <ProgressBar completed={completed} total={total} />}

      {/* Steps toggle (complete mode with result) */}
      {isComplete && resultText && (
        <div className="jx-plan-stepsToggle" onClick={() => setStepsCollapsed(!stepsCollapsed)}>
          <span>{stepsCollapsed ? t('展开步骤详情') : t('收起步骤详情')}</span>
          <motion.span
            style={{ display: 'inline-flex', fontSize: 10 }}
            initial={false}
            animate={{ rotate: stepsCollapsed ? 0 : 90 }}
            transition={{ duration: DUR.fast, ease: EASE.standard }}
          >
            <RightOutlined />
          </motion.span>
        </div>
      )}

      {/* Steps list (collapsible in complete mode, height-auto 0.25s; CollapseHeight defaults to initial=false so it doesn't play on first mount) */}
      <CollapseHeight show={showSteps} duration={0.25}>
        <div className="jx-plan-steps jx-anim-stagger">
          <div className="jx-plan-stepsLabel">
            {mode === 'preview' ? t('执行计划（共 {n} 步）', { n: steps.length }) : t('步骤进度')}
          </div>
          {steps.map((step, idx) => (
            <PlanStepRow
              key={step.step_order ?? idx}
              step={step}
              index={idx}
              mode={mode}
              agentNameMap={agentNameMap}
              defaultExpanded={mode === 'preview' && defaultExpandSteps}
            />
          ))}
        </div>
      </CollapseHeight>

      {/* Execution timeline connector */}
      {isExecuting && isStreaming && (
        <div className="jx-plan-streamingHint">
          <LoadingOutlined spin style={{ fontSize: 12 }} />
          <span>{t('正在执行中...')}</span>
        </div>
      )}

      {/* Footer */}
      {mode === 'preview' && previewFooter !== null && (
        <div className="jx-plan-footer">
          {previewFooter ?? (
            <div className="jx-plan-footerTip">
              {t('请回复')} <strong>"{t('确认执行')}"</strong> {t('开始按步骤执行此计划，或回复其他内容修改需求。')}
            </div>
          )}
        </div>
      )}

      {/* Complete footer: when just completed within this session, enter with delay 0.3s height-auto; on history load (complete on first mount) show directly */}
      {isComplete && (
        <motion.div
          key="completeFooter"
          style={{ overflow: 'hidden' }}
          initial={justCompleted ? { height: 0, opacity: 0 } : false}
          animate={{ height: 'auto', opacity: 1 }}
          transition={{ duration: DUR.normal, ease: EASE.brandOut, delay: 0.3 }}
        >
          <div className="jx-plan-completeFooter">
            <CheckCircleFilled style={{ color: 'var(--color-success)', fontSize: 13 }} />
            <span>{t('执行完成：共 {total} 步，完成 {completed} 步', { total, completed })}</span>
          </div>
        </motion.div>
      )}
    </div>
  );
}
