/**
 * Tool display metadata resolved across host built-ins and plugin contributions.
 *
 * Host-owned tools keep their tables in `constants.ts`; anything a plugin ships
 * describes itself through `tool_meta` in its `plugin.json`. Callers ask here
 * instead of testing tool names, so no display path enumerates plugin tools.
 *
 * These are plain functions rather than hooks: several callers (module-level
 * tables, non-component helpers) need them outside React, and the underlying
 * store is safe to read imperatively.
 */

import { fillTemplate, resolveText } from '../plugin-ui';
import { usePluginUiStore } from '../stores/pluginUiStore';
import { TOOL_ICONS, TOOL_NAME_OVERRIDES } from './constants';

/**
 * 一个工具对外显示的名字：这条链路原先在 ToolCallRow / ToolProgressInline /
 * ToolRunShell 里各抄了一遍，收敛到这里，三处口径不会再走偏。
 * 后端下发的 displayName → 宿主覆盖表 → 会话内动态名（MCP 连接器）→ 裸工具名。
 */
export function resolveToolDisplayName(
  tool: { name: string; displayName?: string },
  dynamicNames: Record<string, string> = {},
): string {
  return tool.displayName
    || TOOL_NAME_OVERRIDES[tool.name]
    || dynamicNames[tool.name]
    || tool.name;
}

/** Icon URL for a tool: plugin contribution → host table → caller's fallback. */
export function resolveToolIcon(toolName: string, fallback = '/icons/knowledge.png'): string {
  const contributed = usePluginUiStore.getState().findToolMeta(toolName)?.contribution.icon;
  return contributed || TOOL_ICONS[toolName] || fallback;
}

/** Human-readable tool name contributed by a plugin, if any. */
export function resolvePluginToolLabel(toolName: string): string {
  const meta = usePluginUiStore.getState().findToolMeta(toolName)?.contribution;
  return meta?.label ? resolveText(meta.label) : '';
}

/**
 * The step line a plugin wants shown while its tool runs.
 *
 * `step_text` may interpolate the call's arguments (`正在分析：{input.chain_name}`),
 * which is what lets a contributed row name the thing being worked on.
 */
export function resolvePluginToolStepText(toolName: string, input: unknown): string {
  const meta = usePluginUiStore.getState().findToolMeta(toolName)?.contribution;
  if (!meta?.step_text) return '';
  const text = resolveText(meta.step_text);
  const vars = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  return fillTemplate(text, vars);
}

/** Presentation for a citation source type contributed by a plugin, if any. */
export function resolvePluginCitationByType(
  sourceType: string,
): { label: string; icon?: string } | null {
  const citation = usePluginUiStore.getState().findCitationByType(sourceType);
  if (!citation) return null;
  return {
    label: resolveText(citation.label, sourceType),
    ...(citation.icon ? { icon: citation.icon } : {}),
  };
}
