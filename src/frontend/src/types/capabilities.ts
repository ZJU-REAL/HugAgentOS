import type { EditionMarketplaceFetcherFields, EditionMarketplaceItemFields } from '../editionMarketplaceTypes';

export interface CatalogItemBase {
  id: string;
  name: string;
  desc: string;
  enabled: boolean;
  tags?: string[];
  detail?: string; // markdown
  /** 'self' = a private item self-added by the current user (owner-isolated), used to show the "mine" badge and delete button */
  owner?: string;
  deletable?: boolean;
  created_at?: string | null;
}

export interface SkillItem extends CatalogItemBase {
  provider?: string;
  version?: string;
  inputs?: string;
  outputs?: string;
  icon?: string;
}

export interface AgentItem extends CatalogItemBase {
  owner?: string;
  model?: string;
  routeHint?: string;
}

// ── Skill Marketplace ────────────────────────────────────────────────────────
// Preloaded installable skills (synced from SkillHub featured). After install they become AdminSkill (user = private / admin = global).
export interface MarketplaceSecretField {
  key: string;
  label: string;
  help?: string;
  required?: boolean;
  placeholder?: string;
}

export interface MarketplaceSkill extends EditionMarketplaceItemFields {
  slug: string;
  entry_name: string;
  display_name: string;
  summary: string;
  category: string;
  tags: string[];
  version: string;
  author: string;
  icon_url?: string;
  source: string;
  source_url?: string;
  downloads: number;
  stars: number;
  featured: boolean;
  requires_api_key: boolean;
  required_secrets: MarketplaceSecretField[];
  installed?: boolean;
  /** Dependency readiness of an installed skill: 'installing' = dependencies installing / 'rejected' = not approved by admin / 'ready' = usable. */
  dep_status?: 'installing' | 'ready' | 'rejected' | null;
  /** Reason filled in by the admin on rejection (optional), surfaced to the user. */
  dep_reason?: string | null;
  /** DB listing record (admin can delete to remove from the marketplace); false for preloaded skills. */
  deletable?: boolean;
  /** Whether listed on the skill marketplace (admin console can delist; delisted items are hidden from users). */
  market_enabled?: boolean;
  /** Built-in default skill (globally resident, always available to everyone): shown as "built-in" in the marketplace, no install flow. */
  builtin?: boolean;
}

export interface MarketplaceSkillDetail extends MarketplaceSkill {
  files: { path: string; size: number }[];
  instructions: string;
}

export interface MarketplaceListResult {
  items: MarketplaceSkill[];
  categories: string[];
}

// ── Plugin: an installable/removable unit packaging skills + MCP ─────────────
export interface PluginRequiredSecret {
  key: string;
  label: string;
  required?: boolean;
}

// Account connection type: once a plugin declares it, the frontend renders the matching account-connection panel on its detail page (OAuth device flow)
export type PluginConnectionType = 'dingtalk' | 'lark';

// Built-in plugin package list item
export interface PluginListItem extends EditionMarketplaceItemFields {
  slug: string;
  name: string;
  version: string;
  description: string;
  category: string;
  icon?: string | null;
  skills_count: number;
  required_secrets: Array<string | PluginRequiredSecret>;
  source: string;
  installed?: boolean;
  market_enabled?: boolean;     // whether listed on the plugin marketplace (admin console can delist; delisted items are hidden from users)
  has_admin_config?: boolean;   // declares admin-level config (provider credentials), configured by the admin on the plugin detail page
}

// Import report: which components were imported successfully / adapted (downgraded) / dropped
export interface PluginImportReport {
  imported: Array<{ type: string; id: string; name: string }>;
  adapted: Array<{ type: string; id: string; name: string; note?: string }>;
  dropped: Array<{ type: string; name: string; reason: string }>;
}

// Plugin admin-level config (provider credentials): configured centrally by the admin on the
// plugin detail page, stored in SystemConfig, shared by all users and read-only on the user
// side. The user view omits value; the admin view includes value (secrets masked).
export interface PluginAdminConfigField {
  key: string;
  label: string;
  secret: boolean;
  description: string;
  is_set: boolean;
  value?: string;   // returned only in the admin view (configured secrets show as ****, non-secrets show the real value)
}

export interface PluginAdminConfig {
  mode: 'any' | 'all' | string;   // any = ready once any field is configured; all = every field required
  group: string;
  hint: string;
  configured: boolean;            // overall readiness (computed per mode)
  fields: PluginAdminConfigField[];
}

// Plugin detail (normalized component manifest, with bodies for pre-install preview)
export interface PluginDetail {
  slug: string;
  name: string;
  version: string;
  description: string;
  category: string;
  icon?: string | null;
  kind: string;
  required_secrets: PluginRequiredSecret[];
  admin_config?: PluginAdminConfig | null;
  connection?: PluginConnectionType | string | null;
  skills: PluginSkillComponent[];
  mcp: PluginMcpComponent[];
  dropped: Array<{ type: string; name: string; reason: string }>;
}

// Installed plugin
export interface InstalledPluginItem {
  install_id: string;
  slug: string;
  name: string;
  version: string;
  description: string;
  category: string;
  icon?: string | null;
  source: string;
  enabled?: boolean;
  /** Hard runtime availability (admin state/dependency readiness), independent of the user's switch. */
  callable?: boolean;
  is_global?: boolean;   // installed globally by the admin (read-only on the user side; cannot be disabled/uninstalled)
  skills: string[];
  mcp: string[];
  /** Names of tools owned by the plugin’s connector components. */
  tools?: string[];
  import_report: PluginImportReport;
  created_at?: string | null;
  has_admin_config?: boolean;   // declares admin-level config (provider credentials)
}

export interface PluginInstallResult {
  install_id: string;
  slug: string;
  name: string;
  kind: string;
  action: string;
  import_report: PluginImportReport;
}

// Skill component inside a plugin (installed detail)
export interface PluginSkillComponent {
  skill_id: string;
  name: string;
  description: string;
  version: string;
  tags: string[];
  enabled: boolean;
  instructions: string;
  files: string[];
  has_secrets: boolean;
}

// MCP component inside a plugin (installed detail)
export interface PluginMcpComponent {
  server_id: string;
  name: string;
  description: string;
  transport: string;
  url?: string | null;
  enabled: boolean;
  needs_runtime: boolean;
  note?: string;
  tools: Array<{ name: string; description: string }>;
}

// Full detail of an installed plugin (with components)
export interface InstalledPluginDetail {
  install_id: string;
  slug: string;
  name: string;
  is_global?: boolean;
  version: string;
  description: string;
  category: string;
  icon?: string | null;
  source: string;
  import_report: PluginImportReport;
  admin_config?: PluginAdminConfig | null;
  connection?: PluginConnectionType | string | null;
  skills: PluginSkillComponent[];
  mcp: PluginMcpComponent[];
}

// Injection interface decoupling the marketplace dialog from the concrete transport (user apiRequest / admin adminFetch).
export interface MarketplaceFetchers extends EditionMarketplaceFetcherFields {
  loadList: () => Promise<MarketplaceListResult>;
  loadDetail: (slug: string) => Promise<MarketplaceSkillDetail>;
  install: (slug: string, secrets: Record<string, string>) => Promise<{ id: string; action?: string }>;
  /** Optional: delete a listed skill from the marketplace (admin-injected only). */
  remove?: (slug: string) => Promise<void>;
  /** Optional: list/delist a marketplace skill (admin-injected only); delisted items are hidden from users. */
  setEnabled?: (slug: string, enabled: boolean) => Promise<void>;
}

// A user's private skill's "apply to list on the skill marketplace" record (pending = under review / approved = listed / rejected).
export interface MarketplaceSubmission {
  submission_id: string;
  slug: string;
  skill_id: string;
  owner_user_id: string;
  submitter_name: string;
  display_name: string;
  summary: string;
  category: string;
  tags: string[];
  version: string;
  note: string;
  status: 'pending' | 'approved' | 'rejected';
  review_note: string;
  reviewed_at?: string | null;
  created_at?: string | null;
  file_count: number;
  // Attached by the admin detail endpoint
  instructions?: string;
  files?: string[];
}

// ── Sub-Agent Marketplace ────────────────────────────────────────────────────
// Preloaded (rewritten from Cherry featured) + community-listed installable sub-agents.
// Install = clone under the user's own name as a private UserAgent, with "install
// dependencies along" for the bound skills/tools.
export interface MarketplaceAgentBindings {
  skill_ids: string[];
  mcp_server_ids: string[];
  plugin_ids: string[];
  kb_ids: string[];
}

export interface MarketplaceAgent extends EditionMarketplaceItemFields {
  slug: string;
  name: string;
  avatar: string;
  summary: string;
  description: string;
  category: string;
  tags: string[];
  version: string;
  author: string;
  source: string;          // builtin | community
  featured: boolean;
  installed?: boolean;
  deletable?: boolean;     // DB listing records are deletable; false for preloaded ones
  market_enabled?: boolean; // admin console can delist
  skill_count: number;
  mcp_count: number;
  plugin_count: number;
  kb_count: number;
}

export interface MarketplaceAgentDetail extends MarketplaceAgent {
  system_prompt: string;
  welcome_message: string;
  suggested_questions: string[];
  bindings: MarketplaceAgentBindings;
}

export interface MarketplaceAgentListResult {
  items: MarketplaceAgent[];
  categories: string[];
}

// Install-clone response: the cloned agent_id + the install-dependencies-along report.
export interface AgentMarketInstallResult {
  agent_id: string;
  slug: string;
  install_report?: {
    bound: string[];
    installed: string[];
    dropped: string[];
    needs_secret: string[];
  };
  message?: string;
}

// Injection interface decoupling the marketplace dialog from the concrete transport (user / admin).
export interface AgentMarketplaceFetchers extends EditionMarketplaceFetcherFields {
  loadList: () => Promise<MarketplaceAgentListResult>;
  loadDetail: (slug: string) => Promise<MarketplaceAgentDetail>;
  install: (slug: string) => Promise<AgentMarketInstallResult>;
  remove?: (slug: string) => Promise<void>;
  setEnabled?: (slug: string, enabled: boolean) => Promise<void>;
}

// A user-built sub-agent's "apply to list on the marketplace" record.
export interface AgentMarketSubmission {
  submission_id: string;
  slug: string;
  agent_id: string;
  owner_user_id: string;
  submitter_name: string;
  name: string;
  avatar?: string;
  description: string;
  summary: string;
  category: string;
  tags: string[];
  version: string;
  note: string;
  status: 'pending' | 'approved' | 'rejected';
  review_note: string;
  reviewed_at?: string | null;
  created_at?: string | null;
  // Attached by the admin detail endpoint
  system_prompt?: string;
  welcome_message?: string;
  suggested_questions?: string[];
  bindings?: MarketplaceAgentBindings;
}

export interface MCPItem extends CatalogItemBase {
  server?: string;
  tools?: string[];
  icon?: string;
  version?: string;
  /** True when this private MCP was installed from the marketplace. */
  marketplace_installed?: boolean;
}
