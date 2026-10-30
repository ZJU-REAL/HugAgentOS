import { Popover } from 'antd';
import { formatTokens, type ContextBreakdown } from '../../utils/contextUsage';
import { t } from '../../i18n';
import { CONTEXT_CATEGORIES, levelOf, useContextUsageView } from './useContextUsageView';

// Ring geometry (SVG user units).
const R = 16;
const CIRC = 2 * Math.PI * R;

interface GaugeContentProps {
  breakdown: ContextBreakdown;
  window: number;
  ratio: number;
  modelName?: string;
  note: string;
}

function GaugeContent({ breakdown, window, ratio, modelName, note }: GaugeContentProps) {
  const pct = Math.round(ratio * 100);
  const rows = CONTEXT_CATEGORIES
    .map((c) => ({ ...c, value: breakdown[c.key] as number }))
    .filter((r) => r.value > 0);

  return (
    <div className="jx-ctxPop">
      <div className="jx-ctxPop-head">
        <span className="jx-ctxPop-title">{t('上下文占用')}</span>
        {modelName && <span className="jx-ctxPop-model" title={modelName}>{modelName}</span>}
      </div>

      <div className="jx-ctxPop-summary">
        <span className="jx-ctxPop-big" data-level={levelOf(ratio)}>{pct}%</span>
        <span className="jx-ctxPop-frac">
          {formatTokens(breakdown.total)} / {formatTokens(window)} tokens
        </span>
      </div>

      {/* Stacked composition bar */}
      <div className="jx-ctxPop-bar" role="img" aria-label={t('上下文占用构成')}>
        {rows.map((r) => {
          const w = (r.value / Math.max(window, breakdown.total)) * 100;
          if (w <= 0) return null;
          return (
            <span
              key={r.key}
              className="jx-ctxPop-seg"
              style={{ width: `${w}%`, background: r.color }}
              title={`${t(r.label)} · ${formatTokens(r.value)}`}
            />
          );
        })}
      </div>

      <div className="jx-ctxPop-rows">
        {rows.map((r) => (
          <div className="jx-ctxPop-row" key={r.key}>
            <span className="jx-ctxPop-dot" style={{ background: r.color }} />
            <span className="jx-ctxPop-label">{t(r.label)}</span>
            <span className="jx-ctxPop-val">{formatTokens(r.value)}</span>
            <span className="jx-ctxPop-share">{Math.round((r.value / window) * 100)}%</span>
          </div>
        ))}
      </div>

      <div className="jx-ctxPop-note">{t(note)}</div>
    </div>
  );
}

/**
 * A small ring gauge shown beside the model selector. Completed calls use the
 * upstream total; only unsent input and explicit fallback states are estimated.
 */
export function ContextGauge({ draftKey }: { draftKey?: string } = {}) {
  const view = useContextUsageView(draftKey);
  if (!view) return null;
  const { breakdown, window, ratio, level, pct, note, modelName } = view;

  return (
    <Popover
      placement="topRight"
      trigger="hover"
      mouseEnterDelay={0.15}
      overlayClassName="jx-ctxPopover"
      content={(
        <GaugeContent
          breakdown={breakdown}
          window={window}
          ratio={ratio}
          modelName={modelName}
          note={note}
        />
      )}
    >
      <button
        type="button"
        className="jx-ctxGauge"
        data-level={level}
        aria-label={t('上下文占用 {pct}%', { pct })}
      >
        <svg className="jx-ctxGauge-ring" viewBox="0 0 36 36" width="20" height="20">
          <circle className="jx-ctxGauge-track" cx="18" cy="18" r={R} fill="none" strokeWidth="3.4" />
          <circle
            className="jx-ctxGauge-fill"
            cx="18"
            cy="18"
            r={R}
            fill="none"
            strokeWidth="3.4"
            strokeLinecap="round"
            style={{ strokeDasharray: `${Math.min(ratio, 1) * CIRC} ${CIRC}` }}
          />
        </svg>
      </button>
    </Popover>
  );
}

export default ContextGauge;
