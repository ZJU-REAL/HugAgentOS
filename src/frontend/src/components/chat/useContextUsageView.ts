import { useMemo } from 'react';
import { useComposerDraft, chatDraftKey } from '../../stores/composerStore';
import { useChatStore, useModelCapabilitiesStore } from '../../stores';
import {
  combineContextUsage,
  computeContextBreakdown,
  getContextWindow,
  type ContextBreakdown,
} from '../../utils/contextUsage';

export type ContextLevel = 'ok' | 'warn' | 'high';

export function levelOf(ratio: number): ContextLevel {
  if (ratio >= 0.85) return 'high';
  if (ratio >= 0.6) return 'warn';
  return 'ok';
}

// Category → display label + accent colour (aligned with design tokens).
export const CONTEXT_CATEGORIES: Array<{ key: keyof ContextBreakdown; label: string; color: string }> = [
  { key: 'messages', label: '对话消息', color: 'var(--color-primary)' },
  { key: 'tools', label: '工具调用', color: 'var(--color-tint-purple)' },
  { key: 'thinking', label: '思考过程', color: 'var(--color-success)' },
  { key: 'files', label: '文件', color: 'var(--color-warning)' },
  { key: 'input', label: '当前输入', color: 'var(--color-tint-green)' },
  { key: 'system', label: '系统提示词与工具定义', color: 'var(--color-text-tertiary)' },
];

/** Context usage of the current chat, shared by the composer gauge and the run monitor. */
export function useContextUsageView(draftKey?: string) {
  const currentChatId = useChatStore((s) => s.currentChatId);
  const messages = useChatStore((s) => (s.currentChatId ? s.store.chats[s.currentChatId]?.messages : undefined));
  const compaction = useChatStore((s) => (
    s.currentChatId ? s.contextCompactions[s.currentChatId] : undefined
  ));
  const contextUsage = useChatStore((s) => (
    s.currentChatId ? s.contextUsages[s.currentChatId] : undefined
  ));
  const { input: draft, uploadedFiles, importedSpaceFiles } = useComposerDraft(draftKey ?? chatDraftKey(currentChatId));

  const model = useModelCapabilitiesStore((s) => {
    const models = s.capabilities.user_selectable_models;
    if (!s.capabilities.user_model_switch_enabled || !models.length) return undefined;
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

  const configuredWindow = model ? model.context_length : mainContextLength;
  const snapshotMatchesModel = (!model || !contextUsage?.modelProviderId
    || model.provider_id === contextUsage.modelProviderId)
    && (!configuredWindow || !contextUsage?.contextWindow || configuredWindow === contextUsage.contextWindow);
  const window = snapshotMatchesModel && contextUsage?.contextWindow
    ? contextUsage.contextWindow
    : getContextWindow(model, model ? 0 : mainContextLength);

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

  const ratio = breakdown.total / window;
  const level = levelOf(ratio);
  const pct = Math.round(ratio * 100);
  const hasPendingInput = !!draft.trim() || stagedFiles.length > 0;
  const note = contextUsage && !snapshotMatchesModel
    ? '已切换模型；占用沿用上一模型快照，下一次调用后重新校准'
    : contextUsage?.exact
    ? hasPendingInput
      ? '已发送内容为上游实测；当前输入与待发送文件为预估'
      : '总数为上游 usage 实测；分类按后端最终请求清单归一'
    : contextUsage?.source === 'compaction_estimate'
      ? '压缩后基线由后端估算；下一次模型调用后按上游 usage 校准'
      : contextUsage
        ? '供应商未返回 usage，当前为后端最终请求估算'
        : '暂无上游实测快照，仅估算当前可见内容；下一次模型调用后校准';

  return { breakdown, window, ratio, level, pct, note, modelName: model?.display_name || contextUsage?.modelName };
}
