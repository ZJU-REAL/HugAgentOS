import type { Catalog, InstalledPluginItem, ToolCall } from '../types';

type Entry = { id: string; name: string; icon?: string; tools?: string[] };
type Plugin = Pick<InstalledPluginItem, 'slug' | 'install_id' | 'name' | 'icon'> & Partial<Pick<InstalledPluginItem, 'skills' | 'mcp' | 'tools'>>;
export type ToolCapability = { kind: 'skill' | 'plugin' | 'connector'; id: string; name: string; icon?: string | null };

/** Only exact, unambiguous identities may supply a capability's logo. */
export function resolveToolCapability(
  tool: Pick<ToolCall, 'name' | 'input' | 'output'>,
  catalog: Pick<Catalog, 'skills' | 'mcp'> | { skills: Entry[]; mcp: Entry[] },
  plugins: Plugin[],
  contributed?: { slug: string; icon?: string },
): ToolCapability | null {
  const pluginLogo = (plugin: Plugin): ToolCapability => ({ kind: 'plugin', id: plugin.slug, name: plugin.name, icon: plugin.icon });
  const uniquePlugin = (matches: Plugin[]) => matches.length === 1 ? matches[0] : undefined;
  const input = tool.input && typeof tool.input === 'object' ? tool.input : {};
  const token = (...values: unknown[]) => values.find((value): value is string => typeof value === 'string' && !!value.trim())?.trim() || '';
  if (tool.name === 'load_skill' || tool.name === 'view_text_file') {
    const parts = token(input.file_path).replaceAll('\\', '/').replace(/^['"]|['"]$/g, '').split('/').filter(Boolean);
    const pathId = parts.at(-1)?.toUpperCase() === 'SKILL.MD' ? parts.at(-2) : undefined;
    let output = tool.output;
    if (typeof output === 'string') {
      try { output = JSON.parse(output); } catch { output = null; }
    }
    const outputId = output && typeof output === 'object' ? (output as Record<string, unknown>).skill_id : undefined;
    const id = tool.name === 'view_text_file' ? token(pathId) : token(input.skill_name, input.skill_id, input.name, pathId, outputId);
    if (!id) return null;
    const matches = catalog.skills.filter((item) => item.id === id);
    const named = matches.length ? matches : catalog.skills.filter((item) => item.name === id);
    const item = named.length === 1 ? named[0] : null;
    const owner = uniquePlugin(plugins.filter((plugin) => plugin.skills?.includes(item?.id || id)));
    if (owner) return pluginLogo(owner);
    if (tool.name === 'view_text_file' && !item) return null;
    return { kind: 'skill', id: item?.id || id, name: item?.name || id, icon: item?.icon };
  }
  if (tool.name === 'load_plugin') {
    const id = token(input.plugin);
    if (!id) return null;
    const exact = plugins.filter((item) => item.slug === id || item.install_id === id);
    const matches = exact.length ? exact : plugins.filter((item) => item.name === id);
    const item = matches.length === 1 ? matches[0] : null;
    return { kind: 'plugin', id: item?.slug || id, name: item?.name || id, icon: item?.icon };
  }
  const owner = uniquePlugin(plugins.filter((plugin) => plugin.tools?.includes(tool.name)));
  if (owner) return pluginLogo(owner);
  if (contributed) {
    const plugin = uniquePlugin(plugins.filter((item) => item.slug === contributed.slug));
    if (plugin) return pluginLogo(plugin);
  }
  const connectors = catalog.mcp.filter((item) => item.tools?.some((name) => name === tool.name));
  if (connectors.length !== 1) return null;
  const item = connectors[0];
  const connectorOwner = uniquePlugin(plugins.filter((plugin) => plugin.mcp?.includes(item.id)));
  if (connectorOwner) return pluginLogo(connectorOwner);
  return { kind: 'connector', id: item.id, name: item.name, icon: item.icon };
}
