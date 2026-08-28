import { useMemo } from 'react';
import { Popover } from 'antd';
import { useChatStore, useFileStore, useModelCapabilitiesStore } from '../../stores';
import {
  combineContextUsage,
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
  { key: 'messages', label: '对话消息', color: 'var(--color-primary)' },
  { key: 'tools', label: '工具调用', color: 'var(--color-tint-purple)' },
  { key: 'thinking', label: '思考过程', color: 'var(--color-success)' },
  { key: 'files', label: '文件', color: 'var(--color-warning)' },
  { key: 'input', label: '当前输入', color: 'var(--color-tint-green)' },
  { key: 'system', label: '系统提示词与工具定义', color: 'var(--color-text-tertiary)' },
];

interface GaugeContentProps {
  breakdown: ContextBreakdown;
  window: number;
  ratio: number;
  modelName?: string;
  note: string;
}

function GaugeContent({ breakdown, window, ratio, modelName, note }: GaugeContentProps) {
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

      <div className="jx-ctxPop-note">{t(note)}</div>
    </div>
  );
}

/**
 * A small ring gauge shown beside the model selector. Completed calls use the
 * upstream total; only unsent input and explicit fallback states are estimated.
 */
export function ContextGauge() {
  const currentChatId = useChatStore((s) => s.currentChatId);
  const messages = useChatStore((s) => (s.currentChatId ? s.store.chats[s.currentChatId]?.messages : undefined));
  const compaction = useChatStore((s) => (
    s.currentChatId ? s.contextCompactions[s.currentChatId] : undefined
  ));
  const contextUsage = useChatStore((s) => (
    s.currentChatId ? s.contextUsages[s.currentChatId] : undefined
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
  // Backend-provided real main-model window, used when switching is disabled.
  const mainContextLength = useModelCapabilitiesStore((s) => s.capabilities.main_context_length || 0);
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

  const window = useMemo(() => {
    const snapshotMatchesModel = !model
      || !contextUsage?.modelProviderId
      || model.provider_id === contextUsage.modelProviderId;
    if (snapshotMatchesModel && contextUsage?.contextWindow) return contextUsage.contextWindow;
    return getContextWindow(model, mainContextLength);
  }, [model, mainContextLength, contextUsage]);

  const breakdown = useMemo(
    () => contextUsage
      ? combineContextUsage(contextUsage, { draft, stagedFiles, attachmentPreviewChars })
      : computeContextBreakdown(messages, {
          draft,
          stagedFiles,
          attachmentPreviewChars,
          compaction,
        }),
    [contextUsage, messages, draft, stagedFiles, attachmentPreviewChars, compaction],
  );

  const hasContent = (messages?.length || 0) > 0 || !!draft.trim() || uploadedFiles.length > 0 || importedSpaceFiles.length > 0;
  if (!currentChatId || !hasContent) return null;

  const ratio = Math.min(breakdown.total / window, 1);
  const level = levelOf(ratio);
  const pct = Math.round(ratio * 100);
  const hasPendingInput = !!draft.trim() || stagedFiles.length > 0;
  const note = contextUsage?.exact
    ? hasPendingInput
      ? '已发送内容为上游实测；当前输入与待发送文件为预估'
      : '总数为上游 usage 实测；分类按后端最终请求清单归一'
    : contextUsage?.source === 'compaction_estimate'
      ? '压缩后基线由后端估算；下一次模型调用后按上游 usage 校准'
      : contextUsage
        ? '供应商未返回 usage，当前为后端最终请求估算'
        : '暂无上游实测快照，仅估算当前可见内容；下一次模型调用后校准';

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
          modelName={model?.display_name || contextUsage?.modelName}
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
            style={{ strokeDasharray: `${ratio * CIRC} ${CIRC}` }}
          />
        </svg>
      </button>
    </Popover>
  );
}

export default ContextGauge;
