import type { StoredSegment, ThinkingBlock } from './messages';

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
  /** 角色额外要求的 extra_config 能力位；为空表示只按 provider_type 匹配。 */
  requires_capability?: string | null;
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

/* ───── Config platform types ───── */

export interface UsageLogEntry {
  /** 混合架构：该行来自云端还是本机执行面（桌面双模式合并视图）。 */
  origin?: 'cloud' | 'local';
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
  thinking?: ThinkingBlock[] | null;
  tool_calls: unknown;
  usage: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number } | null;
  error: unknown;
  metadata?: { segments?: StoredSegment[] } | null;
  created_at: string;
}
