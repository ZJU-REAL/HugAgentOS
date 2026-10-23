import { useCallback } from 'react';
import { useCatalogStore } from '../stores/catalogStore';
import { usePluginStore } from '../stores/pluginStore';
import { usePluginUiStore } from '../stores/pluginUiStore';
import type { ToolCall } from '../types';
import { resolveToolCapability, type ToolCapability } from '../utils/toolCapability';

/** Resolves any number of tools against one read of the catalog and plugin stores. */
export function useToolCapabilityResolver(): (tool: ToolCall) => ToolCapability | null {
  const catalog = useCatalogStore((state) => state.catalog);
  const plugins = usePluginStore((state) => state.installed);
  const contributions = usePluginUiStore((state) => state.items);
  return useCallback((tool: ToolCall) => {
    const owner = contributions.find((item) => item.contributes.tool_meta?.some((meta) => meta.tool === tool.name));
    const meta = owner?.contributes.tool_meta?.find((entry) => entry.tool === tool.name);
    return resolveToolCapability(tool, catalog, plugins, owner ? { slug: owner.slug, icon: meta?.icon } : undefined);
  }, [catalog, plugins, contributions]);
}

export function useToolCapability(tool: ToolCall) {
  return useToolCapabilityResolver()(tool);
}
