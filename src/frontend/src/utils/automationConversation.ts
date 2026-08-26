import type { InstalledPluginItem } from '../types';

export const AUTOMATION_PLUGIN_SLUG = 'automation';

export const AUTOMATION_CHAT_TEMPLATE =
  '我要创建一个定时任务，每【时间间隔】执行【具体任务】';

export interface AutomationPluginReference {
  name: string;
  skillIds: string[];
  mcpIds: string[];
}

export function resolveAutomationPluginReference(
  installed: InstalledPluginItem[],
): AutomationPluginReference | null {
  const plugin = installed.find(
    (item) => item.slug === AUTOMATION_PLUGIN_SLUG && item.enabled !== false,
  );
  if (!plugin) return null;

  return {
    name: plugin.name,
    skillIds: plugin.skills || [],
    mcpIds: plugin.mcp || [],
  };
}
