import { useCatalogStore } from '../stores/catalogStore';
import { usePluginStore } from '../stores/pluginStore';
import { usePluginUiStore } from '../stores/pluginUiStore';
import type { ToolCall } from '../types';
import { resolveToolCapability } from '../utils/toolCapability';

export function useToolCapability(tool: ToolCall) {
  const catalog = useCatalogStore((state) => state.catalog);
  const plugins = usePluginStore((state) => state.installed);
  const contributions = usePluginUiStore((state) => state.items);
  const owner = contributions.find((item) => item.contributes.tool_meta?.some((meta) => meta.tool === tool.name));
  const meta = owner?.contributes.tool_meta?.find((entry) => entry.tool === tool.name);
  return resolveToolCapability(tool, catalog, plugins, owner ? { slug: owner.slug, icon: meta?.icon } : undefined);
}
