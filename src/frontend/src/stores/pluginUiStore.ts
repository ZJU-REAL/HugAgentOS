import { create } from 'zustand';

import { listPluginUiContributions, pluginWebAssetUrl } from '../api';
import type {
  CanvasViewContribution,
  I18nText,
  ModuleContribution,
  PluginContributions,
  ToolMetaContribution,
  ToolViewContribution,
} from '../plugin-ui/types';

/**
 * Registry of everything installed plugins contribute to the interface.
 *
 * The render path asks this store "who renders tool X?" instead of testing tool
 * names, so the host contains no branch naming any particular plugin. Because
 * the backend only returns contributions from plugins that are installed *and*
 * enabled, disabling a plugin withdraws its interface on the next refresh.
 *
 * Mirrors `pluginStore`'s `fetch(force)` idiom: the capability centre forces a
 * refresh after install/uninstall so the chat surface updates immediately.
 */

interface Indexed<T> {
  slug: string;
  contribution: T;
}

/** A canvas slot resolved from either layer. */
export interface CanvasTarget {
  slug: string;
  /** `canvas_views[].id` or `modules[].id` — both address the same tab slot. */
  id: string;
  title: I18nText;
  /** Pointer(s) into the tool's arguments for a more specific tab label. */
  titleFromInput?: string | string[];
}

interface PluginUiState {
  items: PluginContributions[];
  loaded: boolean;
  loading: boolean;
  fetchContributions: (force?: boolean) => Promise<void>;
  /** The view declaration for a tool's result, if any plugin claims it. */
  findToolView: (toolName: string) => Indexed<ToolViewContribution> | null;
  /** Display metadata (label / icon / step text / citation) for a tool. */
  findToolMeta: (toolName: string) => Indexed<ToolMetaContribution> | null;
  /** A contributed canvas view by id. */
  findCanvas: (slug: string, canvasId: string) => Indexed<CanvasViewContribution> | null;
  /** Citation presentation contributed for a source type (badge lookups). */
  findCitationByType: (sourceType: string) => NonNullable<ToolMetaContribution['citation']> | null;
  /**
   * What should occupy the canvas while a tool runs — either an L0 canvas view
   * or an L2 module that declared `surface: "canvas"`. Callers do not care
   * which layer answered, so the two are unified here rather than at each site.
   */
  findCanvasTargetForTool: (toolName: string) => CanvasTarget | null;
  /** An L2 module bound to a tool, or looked up by id. */
  findModuleForTool: (toolName: string) => Indexed<ModuleContribution> | null;
  findModule: (slug: string, moduleId: string) => Indexed<ModuleContribution> | null;
}

/**
 * Rewrite package-relative icon paths to their served URLs.
 *
 * A plugin ships its icons inside its own package (`web/assets/…`), so the
 * declaration carries a relative path; the browser needs the endpoint that
 * serves it. Absolute paths and full URLs are left alone, which lets a plugin
 * still point at a host asset when it wants to.
 */
function resolveIcons(item: PluginContributions): PluginContributions {
  const fix = (icon?: string): string | undefined => {
    if (!icon) return icon;
    if (icon.startsWith('/') || icon.startsWith('http://') || icon.startsWith('https://')) return icon;
    return pluginWebAssetUrl(item.slug, icon);
  };
  const c = item.contributes;
  return {
    ...item,
    contributes: {
      ...c,
      tool_meta: c.tool_meta?.map((meta) => ({
        ...meta,
        icon: fix(meta.icon),
        ...(meta.citation ? { citation: { ...meta.citation, icon: fix(meta.citation.icon) } } : {}),
      })),
      canvas_views: c.canvas_views?.map((view) => ({ ...view, icon: fix(view.icon) })),
      shortcuts: c.shortcuts?.map((shortcut) => ({ ...shortcut, icon: fix(shortcut.icon) })),
      modules: c.modules?.map((module) => ({ ...module, icon: fix(module.icon) })),
    },
  };
}

function search<T>(
  items: PluginContributions[],
  kind: keyof PluginContributions['contributes'],
  predicate: (entry: T) => boolean,
): Indexed<T> | null {
  for (const item of items) {
    const list = (item.contributes[kind] || []) as unknown as T[];
    for (const entry of list) {
      if (predicate(entry)) return { slug: item.slug, contribution: entry };
    }
  }
  return null;
}

export const usePluginUiStore = create<PluginUiState>((set, get) => ({
  items: [],
  loaded: false,
  loading: false,

  fetchContributions: async (force = false) => {
    if ((get().loaded && !force) || get().loading) return;
    set({ loading: true });
    try {
      const items = (await listPluginUiContributions()).map(resolveIcons);
      set({ items, loaded: true, loading: false });
    } catch {
      // Non-fatal: without contributions every tool falls back to the host's
      // built-in renderers and the generic card, exactly as before.
      set({ loading: false, loaded: true });
    }
  },

  findToolView: (toolName) =>
    search<ToolViewContribution>(get().items, 'tool_views', (entry) => entry.tools.includes(toolName)),

  findToolMeta: (toolName) =>
    search<ToolMetaContribution>(get().items, 'tool_meta', (entry) => entry.tool === toolName),

  findCanvas: (slug, canvasId) => {
    const item = get().items.find((candidate) => candidate.slug === slug);
    const entry = (item?.contributes.canvas_views || []).find((view) => view.id === canvasId);
    return entry && item ? { slug: item.slug, contribution: entry } : null;
  },

  findCitationByType: (sourceType) => {
    const found = search<ToolMetaContribution>(
      get().items,
      'tool_meta',
      (entry) => entry.citation?.type === sourceType,
    );
    return found?.contribution.citation ?? null;
  },

  findCanvasTargetForTool: (toolName) => {
    const canvas = search<CanvasViewContribution>(
      get().items,
      'canvas_views',
      (entry) => !!entry.auto_open_on_tools?.includes(toolName),
    );
    if (canvas) {
      return {
        slug: canvas.slug,
        id: canvas.contribution.id,
        title: canvas.contribution.title,
        titleFromInput: canvas.contribution.title_from_input,
      };
    }
    const module = search<ModuleContribution>(
      get().items,
      'modules',
      (entry) => entry.surface === 'canvas' && !!entry.for_tools?.includes(toolName),
    );
    if (module) {
      return { slug: module.slug, id: module.contribution.id, title: module.contribution.title };
    }
    return null;
  },

  findModuleForTool: (toolName) =>
    search<ModuleContribution>(get().items, 'modules', (entry) => !!entry.for_tools?.includes(toolName)),

  findModule: (slug, moduleId) => {
    const item = get().items.find((candidate) => candidate.slug === slug);
    const entry = (item?.contributes.modules || []).find((module) => module.id === moduleId);
    return entry && item ? { slug: item.slug, contribution: entry } : null;
  },
}));
