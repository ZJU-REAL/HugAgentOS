/**
 * Plugin UI contract types — the browser-side mirror of the backend's
 * ``core/services/plugin_ui_contract.py``.
 *
 * Everything a plugin can contribute to the interface is described by these
 * shapes. The host never hard-codes a plugin: it looks a tool up in the
 * registry built from this payload and renders whichever view the plugin named.
 */

/** A display string, or an i18n map keyed by locale (`zh-CN` / `en` / …). */
export type I18nText = string | Record<string, string>;

/** View kinds the material library provides, grouped as in `views/`. */
export const DOCUMENT_VIEWS = ['badge', 'kv', 'list', 'table', 'markdown', 'sections', 'metrics'] as const;
export const ANALYTIC_VIEWS = ['timeseries', 'ranking', 'comparison', 'distribution', 'score', 'timeline'] as const;
export const CONTAINER_VIEWS = ['tree-graph', 'gallery', 'status-list', 'trace', 'link-card', 'tabs'] as const;

export type ViewKind =
  | typeof DOCUMENT_VIEWS[number]
  | typeof ANALYTIC_VIEWS[number]
  | typeof CONTAINER_VIEWS[number];

/** Field mapping: pointers (`$.a.b[]`), literals, or nested view specs. */
export type ViewMap = Record<string, unknown>;

export interface ActionFilter {
  key: string;
  label: I18nText;
  options_from?: string;
}

/**
 * "Click a thing → call a data source → render the result."
 *
 * Available on every view kind, not just the graph canvas: drilling down from
 * a list row, a ranking entry or a gallery card is the same interaction.
 */
export interface ViewAction {
  id: string;
  label: I18nText;
  trigger: 'node' | 'item' | 'primary';
  data_source: string;
  params?: Record<string, string>;
  result?: {
    view: ViewKind;
    map?: ViewMap;
    paged?: boolean;
    /** Upstream's paging parameter names (they disagree: page/pageNum, …). */
    page_param?: string;
    page_size_param?: string;
    page_size?: number;
    /** Drill-panel column headers, worded by the plugin. */
    columns?: I18nText[];
    /** Drill-panel state texts: panel_title / total / pending / loading / empty / sub_empty. */
    texts?: Record<string, I18nText>;
  };
  filters?: ActionFilter[];
  /** Restricts the action to certain tools (ids from other tools may not resolve upstream). */
  enabled_for_tools?: string[];
}

/** A nested view declaration, used by `tabs` and by module fallbacks. */
export interface ChildViewSpec {
  label?: I18nText;
  view?: ViewKind;
  map?: ViewMap;
  actions?: ViewAction[];
}

export interface ToolMetaContribution {
  tool: string;
  label?: I18nText;
  step_text?: I18nText;
  icon?: string;
  citation?: { type: string; label: I18nText; icon?: string };
}

export interface ToolViewContribution {
  tools: string[];
  view: ViewKind;
  map?: ViewMap;
  unwrap?: string[];
  actions?: ViewAction[];
  primary_action?: { open_canvas: string; label?: I18nText; sublabel?: I18nText };
}

export interface CanvasViewContribution {
  id: string;
  view: ViewKind;
  title: I18nText;
  icon?: string;
  map?: ViewMap;
  /** Envelope keys to peel off the tool output before mapping (as tool_views). */
  unwrap?: string[];
  options?: Record<string, string | number | boolean>;
  actions?: ViewAction[];
  auto_open_on_tools?: string[];
  title_from_input?: string | string[];
}

export interface ShortcutContribution {
  id: string;
  label: I18nText;
  icon?: string;
  prompt?: I18nText;
}

/** Data sources reach the browser as id + parameter schema only. */
export interface DataSourceContribution {
  id: string;
  method: 'GET' | 'POST';
  params_schema?: Record<string, { type: string; required?: boolean; default?: unknown }>;
}

/** An L2 module: frontend assets shipped inside the plugin package. */
export interface ModuleContribution {
  id: string;
  entry: string;
  surface: 'canvas' | 'tool_view';
  title: I18nText;
  icon?: string;
  grants: string[];
  for_tools?: string[];
  height?: { mode: 'ratio' | 'fixed' | 'auto'; value: number };
  fallback?: ChildViewSpec;
}

export interface PluginContributions {
  slug: string;
  version: number;
  contributes: {
    tool_meta?: ToolMetaContribution[];
    tool_views?: ToolViewContribution[];
    canvas_views?: CanvasViewContribution[];
    shortcuts?: ShortcutContribution[];
    data_sources?: DataSourceContribution[];
    modules?: ModuleContribution[];
  };
}
