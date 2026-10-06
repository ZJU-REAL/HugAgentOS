import type { EditionMarketplaceItemFields } from '../editionMarketplaceTypes';

// ── MCP Marketplace ─────────────────────────────────────────────────────────
export interface McpMarketAuthField {
  key: string;
  label: string;
  target: 'header' | 'query' | 'url';
  name?: string;
  prefix?: string;
  required: boolean;
  secret: boolean;
  placeholder?: string;
  help_text?: string;
  doc_url?: string;
  methods?: string[];
}

export interface McpMarketAuthMethod {
  id: string;
  type: 'none' | 'token' | 'oauth2';
  label: string;
  scopes?: string[];
  client_registration?: 'dynamic' | 'manual' | 'dynamic_or_manual';
  client_id_required?: boolean;
  client_secret_required?: boolean;
  help_text?: string;
}

export interface McpMarketAuthConfig {
  default_method: string;
  methods: McpMarketAuthMethod[];
  credential_mode: 'auto' | 'installer' | 'admin';
}

export interface McpMarketTool {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export interface McpMarketItem extends EditionMarketplaceItemFields {
  slug: string;
  display_name: string;
  description: string;
  summary: string;
  user_intro: string;
  category: string;
  tags: string[];
  icon?: string;
  publisher_name: string;
  source: 'admin' | 'community';
  version: string;
  version_id: string;
  transport: 'streamable_http' | 'sse';
  url_origin: string;
  auth_schema: McpMarketAuthField[];
  auth_config: McpMarketAuthConfig;
  requires_auth: boolean;
  credentials_managed_by_admin: boolean;
  supports_admin_credentials: boolean;
  requires_user_credentials: boolean;
  tools: McpMarketTool[];
  tool_count: number;
  tool_hash: string;
  listing_notice: {
    discovery_mode?: 'reviewed' | 'per_install';
    install_notice?: string;
    docs_url?: string;
  };
  status: 'active' | 'changed' | 'suspended';
  status_reason?: string;
  last_verified_at?: string | null;
  installed?: boolean;
  deletable?: boolean;
  market_enabled?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface McpMarketListResult {
  items: McpMarketItem[];
  categories: string[];
}

export interface McpMarketSubmission {
  submission_id: string;
  slug: string;
  source_server_id: string;
  owner_user_id: string;
  submitter_name: string;
  display_name: string;
  description: string;
  category: string;
  tags: string[];
  icon?: string;
  version: string;
  transport: 'streamable_http' | 'sse';
  url_origin: string;
  auth_schema: McpMarketAuthField[];
  auth_config: McpMarketAuthConfig;
  tool_count: number;
  tool_hash: string;
  listing_notice: Record<string, unknown>;
  note: string;
  status: 'pending' | 'approved' | 'rejected';
  review_note: string;
  reviewed_at?: string | null;
  created_at?: string | null;
  user_intro?: string;
  url?: string;
  tools?: McpMarketTool[];
}
