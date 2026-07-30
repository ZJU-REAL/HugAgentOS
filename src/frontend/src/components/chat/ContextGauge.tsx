import { useMemo } from 'react';
import { Popover } from 'antd';
import { useChatStore, useFileStore, useModelCapabilitiesStore } from '../../stores';
import {
  computeContextBreakdown,
  getContextWindow,
  formatTokens,
  type ContextBreakdown,
} from '../../utils/contextUsage';
import { t } from '../../i18n';

// Ring geometry (SVG user units).
const R = 16;
const CIRC = 2 * Math.PI * R;

type Level = 'ok' | 'warn' | 'high';

function levelOf(ratio: number): Level {
  if (ratio >= 0.85) return 'high';
  if (ratio >= 0.6) return 'warn';
  return 'ok';
}

// Category → display label + accent colour (aligned with design tokens).
const CATEGORIES: Array<{ key: keyof ContextBreakdown; label: string; color: string }> = [
  { key: 'messages', label: '对话消息', color: '#126DFF' },
  { key: 'tools', label: '工具调用', color: '#8B5CF6' },
  { key: 'thinking', label: '思考过程', color: '#02B589' },
  { key: 'files', label: '文件', color: '#F8AB42' },
  { key: 'input', label: '当前输入', color: '#22C55E' },
  { key: 'system', label: '系统提示词与工具定义', color: '#808080' },
];

interface GaugeContentProps {
  breakdown: ContextBreakdown;
  window: number;
  ratio: number;
  modelName?: string;
}

function GaugeContent({ breakdown, window, ratio, modelName }: GaugeContentProps) {
  const pct = Math.round(ratio * 100);
  const rows = CATEGORIES
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
          const w = Math.min((r.value / window) * 100, 100);
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

      <div className="jx-ctxPop-note">{t('数值为预估，仅供参考')}</div>
    </div>
  );
}

/**
 * A small ring gauge shown beside the model selector that visualises how much
 * of the current model's context window the conversation is estimated to use.
 * Hovering reveals a per-category breakdown (messages / tool calls / files …).
 */
export function ContextGauge() {
  const currentChatId = useChatStore((s) => s.currentChatId);
  const messages = useChatStore((s) => (s.currentChatId ? s.store.chats[s.currentChatId]?.messages : undefined));
  const compaction = useChatStore((s) => (
    s.currentChatId ? s.contextCompactions[s.currentChatId] : undefined
  ));
  const draft = useChatStore((s) => s.input);
  const uploadedFiles = useFileStore((s) => s.uploadedFiles);
  const importedSpaceFiles = useFileStore((s) => s.importedSpaceFiles);

  const model = useModelCapabilitiesStore((s) => {
    const models = s.capabilities.user_selectable_models;
    if (!models.length) return undefined;
    return (
      models.find((m) => m.provider_id === s.selectedModelProviderId)
      || models.find((m) => m.is_default)
      || models[0]
    );
  });
  // Backend-provided real values: main model window (used when no model is
  // explicitly selected / switching disabled) and the system-prompt reserve.
  const mainContextLength = useModelCapabilitiesStore((s) => s.capabilities.main_context_length || 0);
  const systemPromptTokens = useModelCapabilitiesStore((s) => s.capabilities.system_prompt_tokens || 0);
  const attachmentPreviewChars = useModelCapabilitiesStore(
    (s) => s.capabilities.attachment_preview_chars || 0,
  );

  const stagedFiles = useMemo(
    () => [
      ...uploadedFiles.map((f) => ({ name: f.name, type: f.type, size: f.size })),
      ...importedSpaceFiles.map((f) => ({ name: f.name, type: f.mime_type })),
    ],
    [uploadedFiles, importedSpaceFiles],
  );

  const window = useMemo(
    () => getContextWindow(model, mainContextLength),
    [model, mainContextLength],
  );

  const breakdown = useMemo(
    () => computeContextBreakdown(messages, {
      draft,
      stagedFiles,
      attachmentPreviewChars,
      systemTokens: systemPromptTokens,
      compaction,
    }),
    [messages, draft, stagedFiles, attachmentPreviewChars, systemPromptTokens, compaction],
  );

  const hasContent = (messages?.length || 0) > 0 || !!draft.trim() || uploadedFiles.length > 0 || importedSpaceFiles.length > 0;
  if (!currentChatId || !hasContent) return null;

  const ratio = Math.min(breakdown.total / window, 1);
  const level = levelOf(ratio);
  const pct = Math.round(ratio * 100);

  return (
    <Popover
      placement="topRight"
      trigger="hover"
      mouseEnterDelay={0.15}
      overlayClassName="jx-ctxPopover"
      content={<GaugeContent breakdown={breakdown} window={window} ratio={ratio} modelName={model?.display_name} />}
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
            style={{ strokeDasharray: `${ratio * CIRC} ${CIRC}` }}
          />
        </svg>
      </button>
    </Popover>
  );
}

export default ContextGauge;
