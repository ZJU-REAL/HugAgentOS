import type { ToolCall } from './types';

export const EDITION_TOOL_NAME_OVERRIDES: Record<string, string> = {};
export const EDITION_STEP_ICONS = {};
export const EDITION_API_CATEGORY_RULES: Array<{ test: RegExp; group: string }> = [];

export function getEditionToolRowLabel(
  _tool: ToolCall,
): { prefix: string; value: string; count?: number } | null {
  return null;
}
