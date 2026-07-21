export type PanelKey = 'chat' | 'skills' | 'agents' | 'mcp' | 'kb' | 'docs' | 'app_center' | 'settings' | 'share_records' | 'my_space' | 'ability_center' | 'lab' | 'projects' | 'project_detail';

// ─── Projects (Claude-style workspaces) ─────────────────────────────────────

export type ProjectKind = 'personal' | 'team';
export type ProjectPermission = 'admin' | 'edit' | 'view' | 'none';
export type ProjectFileSource = 'upload' | 'reference';

export interface ProjectItem {
  project_id: string;
  name: string;
  description: string;
  kind: ProjectKind;
  owner_user_id: string;
  team_id: string | null;
  team_name: string | null;
  /** user_folder.folder_id a personal project is linked to */
  linked_folder_id: string | null;
  /** team_folder.folder_id a team project is linked to */
  linked_team_folder_id: string | null;
  /** Linked folder name (for frontend display) */
  folder_name: string | null;
  instructions: string;
  icon_color: string | null;
  pinned: boolean;
  favorite: boolean;
  /** Project-level memory read switch (whether in-project sessions can retrieve / display project memories; default ON) */
  memory_enabled: boolean;
  /** Project-level memory write switch (whether project memories are extracted and written after an in-project session ends; default ON) */
  memory_write_enabled: boolean;
  permission: ProjectPermission;
  file_count: number;
  chat_count: number;
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  last_activity_at: string | null;
}

export interface ProjectDetail extends ProjectItem {
  capacity_used?: number;
  capacity_limit?: number;
}

/**
 * Project file = an artifact under the linked MySpace folder subtree.
 *
 * - ``id`` == ``artifact_id`` (there is no longer a separate project_files table)
 * - ``name`` is the ``subdir/file.ext`` path relative to the linked folder
 * - ``folder_path`` is the directory prefix of ``name`` with the filename removed, for the frontend to group by folder
 */
export interface ProjectFileItem {
  id: string;
  artifact_id: string;
  name: string;            // ``subfolder/file.ext`` or ``file.ext``
  title: string;
  mime_type: string;
  size_bytes: number;
  download_url: string;
  type: string;
  folder_path?: string;    // ``subfolder`` or ``''``
  created_at: string | null;
}

export type ChatShareScope = 'private' | 'team_read' | 'team_edit';

export interface ProjectChatSummary {
  chat_id: string;
  title: string;
  /** team-share scenario: from chat_session_user_states, independent per member. */
  pinned: boolean;
  favorite: boolean;
  message_count: number;
  last_message_at: string | null;
  updated_at: string | null;
  created_at: string | null;
  /** Session creator's user_id. */
  owner_user_id?: string;
  /** Session creator's display name. */
  owner_name?: string | null;
  /** Session-level sharing switch (only effective in team projects with the project-level sharing switch ON). */
  share_scope?: ChatShareScope;
  /** Whether the current user is the session owner. */
  is_owner?: boolean;
}

export interface TeamForProjectCreation {
  team_id: string;
  name: string;
  role: 'owner' | 'admin';
}

export type CitationSourceType =
  | 'internet'
  | 'knowledge_base'
  | 'database'
  | 'industry_news'
  | 'ai_news'
  | 'chain_info'
  | 'company_profile'
  | 'unknown';

export interface CitationItem {
  id: string;            // e.g. "internet_search-1", "retrieve_dataset_content-2"
  tool_name: string;
  tool_id?: string;
  title: string;
  url: string;
  snippet: string;
  source_type: CitationSourceType;
}

export type UpdateCategory = '模型迭代' | '信息处理' | '应用上新' | '体验优化';

export interface UpdateEntry {
  date: string;
  year: string;
  title: string;
  category: UpdateCategory;
  desc: string;
}

export type OntologyAssetKind = 'tool' | 'skill' | 'subagent';

export interface OntologyTagPack {
  pack_id: string;
  pack_name: string;
  domain: string;
  version: string;
}

export interface OntologyTagWorkflow {
  workflow_ref: string;
  workflow_name: string;
  review_level: string;
  risk: string;
}

/** A controlled tag declared by an active Domain Pack as an asset workflow trigger. */
export interface OntologyTagOption {
  value: string;
  concept_id: string;
  concept_name: string;
  definition: string;
  risk: string;
  packs: OntologyTagPack[];
  workflows: OntologyTagWorkflow[];
}

export interface CapItem {
  title: string;
  desc: string;
  bullets: string[];
}

export type ChatRole = 'user' | 'assistant';

/**
 * One streaming sub-step inside a sub-agent (call_subagent) — rendered under the
 * parent call_subagent tool card. Delivered by the backend via the `subagent_event`
 * SSE event, correlated by parent_tool_id.
 */
export interface SubagentStep {
  kind: 'tool' | 'thinking' | 'content';
  // When kind === 'tool': one tool call made by the sub-agent
  toolId?: string;
  name?: string;
  displayName?: string;
  input?: any;
  output?: any;
  status?: 'running' | 'success' | 'error';
  // When kind === 'thinking' | 'content': the accumulated text
  text?: string;
}

export interface ToolCall {
  id?: string;
  name: string;
  displayName?: string;
  input?: any;
  output?: any;
  status?: 'pending' | 'running' | 'success' | 'error';
  timestamp?: number;
  // call_subagent only: the sub-agent's internal streaming sub-steps + the sub-agent's name
  subSteps?: SubagentStep[];
  subagentName?: string;
  scope?: 'ontology_revision' | string;
}

/** §13 MySpace write confirmation decision (literal counterparts of the backend's _myspace_confirm.DECISION_*). */
export type FileConfirmDecision = 'allow' | 'allow_session' | 'deny';

/** §13 MySpace write / automation-task change confirmation: pending-confirmation info (delivered by the backend via the file_confirm SSE event). */
export interface FileConfirmInfo {
  confirmId: string;
  op: string;
  logicalPath: string;
  message?: string;
  /** Confirmation category: 'myspace' (MySpace write, default) | 'automation' (automation-task change). */
  kind?: string;
}

/** Site-building design pick (choose 1 of 3): a single candidate option (registered by the backend choose_design tool). */
export interface DesignPickOption {
  id: string;
  title: string;
  brief?: string;
  /** artifact file_id of the preview screenshot; the frontend displays it inline via /files/{id}?inline=true. */
  imageFileId: string;
}

/** Site-building design pick (choose 1 of 3): pending-selection info (delivered by the backend via the design_pick SSE event). */
export interface DesignPickInfo {
  confirmId: string;
  question: string;
  options: DesignPickOption[];
}

export interface ThinkingBlock {
  content: string;
  timestamp?: number;
}

export interface OntologyActivationSummary {
  pack_id?: string;
  workflow_id?: string;
  workflow_name?: string;
  source?: 'text' | 'tool' | 'skill' | 'subagent' | string;
  asset_kind?: string;
  asset_id?: string;
  review_level?: string;
}

export interface OntologyGateSummary {
  decision?: 'pass' | 'deny' | string;
  tool_name?: string;
  matched_rule_ids?: string[];
  violations?: string[];
  denial_count?: number;
  circuit_breaker?: boolean;
}

export interface OntologyReviewSummary {
  status?: 'pending' | 'running' | 'completed' | 'failed' | string;
  level?: 'none' | 'checkpoint' | 'committee' | string;
  owner?: string | null;
  count?: number;
  verdict?: 'pass' | 'revise' | 'escalate' | string;
  revised?: boolean;
  latency_ms?: number;
  committee_size?: number;
  candidate_answer?: string;
  accepted?: boolean;
  violations?: Array<Record<string, unknown>>;
  affected_claims?: OntologyManualReviewItem[];
  evidence?: string[];
  feedback?: string[];
  error?: string;
  manual_review?: OntologyManualReview;
  new_tools?: string[];
  new_citation_count?: number;
}

export interface OntologyManualReviewItem {
  quote: string;
  rule_id: string;
  risk: string;
  manual_check: string;
}

export interface OntologyManualReview {
  required: boolean;
  title: string;
  summary: string;
  items: OntologyManualReviewItem[];
  actions: string[];
}

export interface OntologyRevisionSummary {
  status: 'pending' | 'streaming' | 'completed' | string;
  source?: string;
  content: string;
  thinking: ThinkingBlock[];
  toolCalls: ToolCall[];
  toolCallCount?: number;
  toolPending?: boolean;
}

/** User-visible ontology governance evidence, intentionally separate from model thinking. */
export interface OntologyGovernanceSummary {
  governance_run_id?: string;
  activations: OntologyActivationSummary[];
  gates: OntologyGateSummary[];
  review: OntologyReviewSummary;
  revision?: OntologyRevisionSummary;
}

/** Records the order of a message's elements (text/tool calls/thinking) for inline interleaved rendering */
export interface MessageSegment {
  type: 'text' | 'tool' | 'thinking' | 'plan';
  content?: string;    // used for 'text' and 'thinking' types
  toolIndex?: number;  // used for 'tool' type; refers to toolCalls[toolIndex]
  planData?: {         // used for 'plan' type
    mode: 'preview' | 'executing' | 'complete';
    planId?: string;   // associated plan_id — used to restore the "pending-confirmation plan" from history messages after refresh
    title: string;
    description?: string;
    steps: Array<{
      step_order: number;
      title: string;
      description?: string;
      expected_tools?: string[];
      expected_skills?: string[];
      expected_agents?: string[];
      acceptance_criteria?: string;
      status?: 'pending' | 'running' | 'success' | 'failed' | 'skipped';
      summary?: string;
      text?: string;
    }>;
    completedSteps?: number;
    totalSteps?: number;
    resultText?: string;
    agentNameMap?: Record<string, string>;
  };
}

export interface ChatMessage {
  role: ChatRole;
  content: string;
  isMarkdown?: boolean;
  ts: number;
  quotedFollowUp?: {
    text: string;
    ts?: number;
  };
  skillId?: string;
  skillName?: string;
  pluginName?: string;
  mentionName?: string;
  messageId?: string;   // backend message_id, used for feedback submission
  toolCalls?: ToolCall[];
  thinking?: ThinkingBlock[];
  ontologyGovernance?: OntologyGovernanceSummary;
  segments?: MessageSegment[];  // ordered segment list (used by new messages)
  citations?: CitationItem[];   // tool-call citation registry
  followUpQuestions?: string[]; // follow-up questions (clickable to send)
  isStreaming?: boolean;
  /** Total generation duration of this agent answer (ms): total wall-clock time from
   *  initiating the answer to the end of streaming output. Only carried by locally
   *  newly-generated assistant messages; backend history messages lack this field. Used
   *  to show "took X.Xs" next to "regenerate" in the action bar. */
  durationMs?: number;
  /** Backend signals an extended LLM silence; UI replaces streaming dots with a "正在准备调用工具…" indicator. */
  toolPending?: boolean;
  /** Wall-clock ms of the last streaming activity (content / tool / meta event).
   *  Anchors the "正在准备调用工具…" elapsed timer to a persisted value so it
   *  survives component remounts on session switch / page refresh instead of
   *  resetting to zero. */
  lastActivityTs?: number;
  attachments?: Array<{
    name: string;
    mime_type?: string;
    file_id?: string;       // OSS file ID; downloadable when present
    download_url?: string;  // download path, e.g. /files/{file_id}
  }>;  // files uploaded by the user
  /**
   * Workspace allowlist: file_ids the agent pinned via pin_to_workspace.
   * When set, only these file_ids render as artifact cards (everything else
   * from tool outputs is hidden). When undefined/null, legacy behavior
   * (show every file_id extracted from tool outputs).
   */
  workspaceFiles?: string[] | null;
}

export interface ChatItem {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  messages: ChatMessage[];
  favorite?: boolean;
  pinned?: boolean;
  businessTopic?: string;
  /** Sub-agent binding (set when chat is started from a sub-agent) */
  agentId?: string;
  agentName?: string;
  /** Whether this chat was created via plan mode from the App Center */
  planChat?: boolean;
  /** Whether this chat was created via the batch-execution ("批量执行") entry from the App Center */
  batchChat?: boolean;
  /** Whether this chat was created via the site-building ("站点建站") entry (Lab → Sites) */
  siteChat?: boolean;
  /** Automation task ID — set on virtual sidebar entries for automation tasks */
  automationTaskId?: string;
  /** Whether this is an automation-generated chat (virtual sidebar entry) */
  automationRun?: boolean;
  /** Which project this chat is mounted under (Claude-style workspaces).
   *  When present, sending a message automatically attaches project_id so the backend
   *  injects the project instructions / folder scope into ctx. */
  projectId?: string;
  /** Display name of the chat's project (cached at binding time, used for the "project name / title" breadcrumb in the chat header). */
  projectName?: string;
  /** Session-level sharing switch (only effective in team projects with the project-level sharing switch ON). */
  shareScope?: ChatShareScope;
  /** Session creator's user_id (sharing scenarios need to show "created by: xxx"). */
  ownerUserId?: string;
  /** Session creator's display name. */
  ownerName?: string;
  /** The current user's access level for this chat (sharing scenarios). */
  accessLevel?: 'admin' | 'edit' | 'read';
  /** Whether the current user is the creator of this chat. */
  isOwner?: boolean;
}

export interface ChatStore {
  chats: Record<string, ChatItem>;
  order: string[];
}

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

// ── Marketplace visibility scope (shared by all three marketplaces: skills/plugins/sub-agents) ──
export type MarketVisibilityValue = 'public' | 'scoped';

export interface MarketVisibilityGrant {
  principal_type: 'user' | 'team' | 'role';
  principal_id: string;
}

export interface MarketVisibilityConfig {
  visibility: MarketVisibilityValue;
  grants: MarketVisibilityGrant[];
}

// Data source for the visibility-scope picker (/v1/admin/visibility/principals)
export interface VisibilityPrincipals {
  users: Array<{ user_id: string; username: string; real_name?: string }>;
  teams: Array<{ team_id: string; name: string }>;
  roles: Array<{ role_id: string; name: string }>;
}

// Visibility-scope config transport (admin-injected only; the three marketplaces share one dialog component)
export interface VisibilityScopeFetchers {
  getVisibility: (slug: string) => Promise<MarketVisibilityConfig>;
  setVisibility: (slug: string, config: MarketVisibilityConfig) => Promise<void>;
  loadPrincipals: () => Promise<VisibilityPrincipals>;
}

export interface MarketplaceSkill {
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
  /** Visibility scope: public = visible to everyone (default); scoped = visible only to authorized users/teams/roles. */
  visibility?: MarketVisibilityValue;
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
export interface PluginListItem {
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
  visibility?: MarketVisibilityValue; // visibility scope: public = everyone (default); scoped = authorized principals only
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
  is_global?: boolean;   // installed globally by the admin (read-only on the user side; cannot be disabled/uninstalled)
  skills: string[];
  mcp: string[];
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
export interface MarketplaceFetchers {
  loadList: () => Promise<MarketplaceListResult>;
  loadDetail: (slug: string) => Promise<MarketplaceSkillDetail>;
  install: (slug: string, secrets: Record<string, string>) => Promise<{ id: string; action?: string }>;
  /** Optional: delete a listed skill from the marketplace (admin-injected only). */
  remove?: (slug: string) => Promise<void>;
  /** Optional: list/delist a marketplace skill (admin-injected only); delisted items are hidden from users. */
  setEnabled?: (slug: string, enabled: boolean) => Promise<void>;
  /** Optional: visibility-scope config (admin-injected only); once configured, user-side display is filtered by user/team/role. */
  visibility?: VisibilityScopeFetchers;
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

export interface MarketplaceAgent {
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
  visibility?: MarketVisibilityValue; // visibility scope: public = everyone (default); scoped = authorized principals only
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
export interface AgentMarketplaceFetchers {
  loadList: () => Promise<MarketplaceAgentListResult>;
  loadDetail: (slug: string) => Promise<MarketplaceAgentDetail>;
  install: (slug: string) => Promise<AgentMarketInstallResult>;
  remove?: (slug: string) => Promise<void>;
  setEnabled?: (slug: string, enabled: boolean) => Promise<void>;
  /** Optional: visibility-scope config (admin-injected only); once configured, user-side display is filtered by user/team/role. */
  visibility?: VisibilityScopeFetchers;
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
}

export interface KBDocument {
  id: string;
  title: string;
  desc?: string;
  content?: string;
  indexing_status?: string;  // "processing" | "completed" | "failed"
  word_count?: number;
  size_bytes?: number;
  created_at?: number;
}

export interface KBChunk {
  chunk_id: string;
  document_id: string;
  chunk_index: number;
  content: string;
  tags: string[];
  questions: string[];
}

export interface KBItem extends CatalogItemBase {
  provider?: string;
  version?: string;
  inputs?: string;
  outputs?: string;
  documents?: KBDocument[];
  visibility?: 'public' | 'private';
  is_public?: boolean;
  document_count?: number;
  chunk_method?: string;
  system_managed?: boolean;
  pinned?: boolean;
  editable?: boolean;
  deletable?: boolean;
  uploadable?: boolean;
}

export interface ChunkPreviewChild {
  index: number;
  content: string;
}

export interface ChunkPreviewItem {
  index: number;
  content: string;
  token_count: number;
  children_count: number;
  children_preview: ChunkPreviewChild[];
}

export interface ChunkPreviewResult {
  total_chunks: number;
  total_children: number;
  chunks: ChunkPreviewItem[];
}

export interface MemoryItem {
  id: string;
  memory: string;
  created_at?: string;
  updated_at?: string;
  score?: number;
  // metadata fields newly flattened by the backend (may be missing; kept compatible with old data)
  layer?: 'L1' | 'L2' | 'L3' | 'session';
  source?: string;
  tags?: string[];
  confidentiality?: 'public' | 'internal' | 'sensitive';
  ttl_days?: number;
  evidence?: string;
}

export interface MemoryProfile {
  enabled: boolean;
  workspace_id: string;
  content_md: string;
  length: number;
  max_chars: number;
}

export interface MemoryGraphRelation {
  source: string;
  relationship: string;
  target: string;
}

export interface Catalog {
  skills: SkillItem[];
  agents: AgentItem[];
  mcp: MCPItem[];
  kb: KBItem[];
}

// ── Model management types ──────────────────────────────────────────────────

export type ProviderType = 'chat' | 'embedding' | 'reranker';

export interface ProviderField {
  key: string;
  label: string;
  required: boolean;
  secret: boolean;
  placeholder: string;
}

export interface ProviderSpec {
  id: string;
  label: string;
  engine: 'openai' | 'native' | 'litellm';
  supports_types: ProviderType[];
  base_url_template: string;
  autofill_base_url: boolean;   // true → auto-fill base_url_template into the input box when this provider is selected
  api_key_required: boolean;
  fields: ProviderField[];
}

export interface ModelProvider {
  provider_id: string;
  display_name: string;
  provider_type: ProviderType;
  provider: string;        // vendor/protocol id (see backend core/llm/providers/registry.py)
  base_url: string;
  api_key: string;       // masked in responses
  model_name: string;
  extra_config: Record<string, unknown>;
  is_active: boolean;
  gateway_group?: string | null;  // external gateway "model group": multiple providers in the same group are merged at the gateway into multiple upstreams of one external alias (LB/failover)
  weight?: number;                // weighted round-robin weight within the pool (default 1)
  priority?: number;              // reserved primary/backup semantics (default 0)
  input_price: number | null;   // ¥/1K input tokens (from model_pricing, joined by model_name)
  output_price: number | null;  // ¥/1K output tokens
  currency: string;
  last_tested_at: string | null;
  last_test_status: 'success' | 'failure' | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ModelRole {
  role_key: string;
  label: string;
  required_type: ProviderType;
  provider_id: string | null;
  provider_name: string | null;
  model_name: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface TestConnectionResult {
  success: boolean;
  latency_ms: number;
  error: string | null;
}

// ── Service configuration types ────────────────────────────────────────────

export interface SystemConfig {
  config_key: string;
  config_value: string | null;
  display_name: string;
  description: string | null;
  group_key: string;
  is_secret: boolean;
  updated_at: string | null;
  updated_by: string | null;
}

export interface SystemConfigGroup {
  group_key: string;
  label: string;
  items: SystemConfig[];
}

// ── My Space types ─────────────────────────────────────────────────────────

export type MySpaceTab = 'assets' | 'favorites' | 'shares' | 'notifications';

// ── Automation types ────────────────────────────────────────────
export type AutomationTaskType = 'prompt' | 'plan';
export type AutomationStatus = 'active' | 'paused' | 'disabled' | 'completed' | 'expired';
export type AutomationRunStatus = 'running' | 'success' | 'failed';
export type AutomationScheduleType = 'recurring' | 'once' | 'manual';

export interface AutomationTask {
  task_id: string;
  task_type: AutomationTaskType;
  prompt?: string;
  plan_id?: string;
  plan_title?: string;
  cron_expression: string;
  schedule_type: AutomationScheduleType;
  timezone: string;
  name?: string;
  description?: string;
  status: AutomationStatus;
  next_run_at?: string;
  last_run_at?: string;
  run_count: number;
  max_runs?: number;
  consecutive_failures: number;
  max_failures: number;
  last_error?: string;
  enabled_mcp_ids: string[];
  enabled_skill_ids: string[];
  enabled_kb_ids: string[];
  enabled_agent_ids: string[];
  sidebar_activated?: boolean;
  /** Channel delivery target (echoed back when editing): absent means in-app only */
  channel_id?: string | null;
  conversation_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AutomationChatGroup {
  taskId: string;
  taskName: string;
  runs: AutomationRun[];
  latestCompletedChatId: string | null;
  latestRunAt: number;
}

export interface AutomationRun {
  run_id: string;
  task_id: string;
  status: AutomationRunStatus;
  chat_id?: string;
  result_summary?: string;
  error_message?: string;
  started_at: string;
  completed_at?: string;
  duration_ms?: number;
  usage?: Record<string, unknown>;
}

export interface AutomationNotification {
  id: string;
  task_id: string;
  task_name: string;
  status: 'success' | 'failed';
  summary: string;
  chat_id?: string;
  timestamp: number;
  read: boolean;
}

export interface ResourceItem {
  id: string;
  type: 'document' | 'image' | 'favorite';
  name: string;
  mime_type?: string;
  file_id?: string;
  download_url?: string;
  size?: number;
  source_kind?: 'user_upload' | 'ai_generated';
  knowledge_base_count?: number;
  knowledge_bases?: Array<{ kb_id: string; name: string }>;
  source_chat_id?: string;
  source_chat_title?: string;
  content_preview?: string;
  created_at: string;
  // Folder membership (only meaningful for the assets type)
  team_id?: string | null;
  team_folder_id?: string | null;
  user_folder_id?: string | null;
}

/** Personal folder node (the tree structure under "MySpace"). */
export interface PersonalFolderNode {
  folder_id: string;
  parent_folder_id: string | null;
  name: string;
  created_at?: string | null;
  children: PersonalFolderNode[];
}

// ── Plan Mode types ───────────────────────────────────────────────────────

export type PlanStatus = 'draft' | 'approved' | 'running' | 'completed' | 'failed' | 'cancelled';
export type PlanStepStatus = 'pending' | 'running' | 'success' | 'failed' | 'skipped';

export interface PlanStep {
  step_id: string;
  step_order: number;
  title: string;
  description: string;
  expected_tools: string[];
  expected_skills: string[];
  expected_agents: string[];
  status: PlanStepStatus;
  result_summary?: string;
  tool_calls?: ToolCall[];
  ai_output?: string;
  error_message?: string;
  started_at?: string;
  completed_at?: string;
}

export interface Plan {
  plan_id: string;
  title: string;
  description: string;
  task_input: string;
  status: PlanStatus;
  total_steps: number;
  completed_steps: number;
  result_summary?: string;
  steps: PlanStep[];
  created_at: string;
  updated_at: string;
}

/* ───── Config platform types ───── */

export interface UsageLogEntry {
  message_id: string;
  chat_id: string;
  user_id: string;
  username: string;
  session_title: string;
  model: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  has_error: boolean;
  created_at: string;
}

export interface UsageSummaryItem {
  group_key: string;
  display_name?: string;
  total_requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface BillingSummaryItem {
  group_key: string;
  display_name?: string;
  total_requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  prompt_cost: number;
  completion_cost: number;
  total_cost: number;
  currency: string;
}

export interface ModelPricingItem {
  pricing_id: string;
  model_name: string;
  display_name: string | null;
  input_price: number;
  output_price: number;
  currency: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface AdminChatSession {
  chat_id: string;
  user_id: string;
  username: string;
  title: string;
  message_count: number;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface AdminChatMessage {
  message_id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  model: string | null;
  tool_calls: unknown;
  usage: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number } | null;
  error: unknown;
  metadata: unknown;
  created_at: string;
}

// ── Batch execution feature ────────────────────────────────────────────────

export type BatchSourceType = 'xlsx' | 'word_files' | 'text_list';

export interface BatchPlanMeta {
  plan_id: string;
  total: number;
  source_type: BatchSourceType;
  preview: Record<string, unknown>[];
  default_template: string;
  placeholder_keys: string[];
  chat_id?: string;
  warnings?: string[];   // truncation / cap warnings surfaced from backend
}

export interface BatchItemResult {
  index: number;
  total?: number;
  status: 'success' | 'skipped';
  content?: string;
  error?: string;
  retry_count: number;
  progress?: { done: number; success: number; failed: number };
  // Optional captured side-channels from the per-item sub-agent run.
  // Lets the frontend reuse the chat-bubble's tool-call / artifact /
  // citation primitives for diverse output formats (charts, Word/Excel
  // exports, KB-cited research) without bespoke rendering.
  tool_calls?: ToolCall[];
  artifacts?: unknown[];
  citations?: CitationItem[];
}

export interface BatchPlanState {
  meta: BatchPlanMeta;
  template?: string;        // user-edited template (after confirm)
  status: 'awaiting_confirm' | 'running' | 'done' | 'cancelled' | 'error';
  results: BatchItemResult[];
  startedAt?: number;
  finishedAt?: number;
  errorMsg?: string;
  summary?: { total: number; success: number; failed: number };
}

// ── Autonomous Loop (long-running autonomous operation) ─────────────────────
export interface LoopGoalSpec {
  objective: string;
  /** Acceptance criteria (optional; if left empty the backend extracts them from objective).
   *  Judgment is done by a read-only reviewer sub-agent verifying the actual output itself;
   *  there are no more scripted verification fields such as verify_cmd / numeric scores /
   *  thresholds (removed wholesale). */
  acceptance_criteria?: string[];
}
export interface LoopBudget {
  max_iters: number;
  max_wall_clock_s: number;
  max_tokens: number;
}
export interface LoopItem {
  loop_id: string;
  title: string;
  status: string;
  goal_spec: LoopGoalSpec;
  budget: LoopBudget;
  iteration_count: number;
  tokens_spent: number;
  final_score: number | null;
  result_summary?: string | null;
  chat_id?: string | null;
  created_at?: string | null;
}
export interface LoopIterationItem {
  seq: number;
  verdict: string;
  score: number | null;
  reasoning?: string;
  tool_calls: number;
  tokens: number;
  decided_by?: string;
}
