import { create } from 'zustand';

export interface AgentChangeHistoryItem {
  version: string;
  timestamp: string;
  content: string;
  operator_name: string;
  details: Array<{
    field: string;
    before: string;
    after: string;
  }>;
}

export interface UserAgentItem {
  agent_id: string;
  owner_type: 'admin' | 'user' | 'team';
  user_id: string | null;
  team_id: string | null;
  name: string;
  avatar: string | null;
  description: string;
  system_prompt: string;
  welcome_message: string;
  suggested_questions: string[];
  mcp_server_ids: string[];
  skill_ids: string[];
  plugin_ids: string[];
  kb_ids: string[];
  model_provider_id: string | null;
  temperature: number | null;
  max_tokens: number | null;
  max_iters: number;
  timeout: number;
  is_enabled: boolean;
  sort_order: number;
  extra_config: Record<string, unknown>;
  version: string;
  change_history: AgentChangeHistoryItem[];
  created_at: string | null;
  updated_at: string | null;
  created_by: string | null;
}

export interface AvailableResources {
  mcp_servers: Array<{ id: string; name: string; description: string; enabled: boolean }>;
  skills: Array<{ id: string; name: string; description: string }>;
  plugins: Array<{ id: string; name: string; description: string; skill_count: number; mcp_count: number }>;
}

interface AgentState {
  /** All agents visible to current user (admin + own) */
  agents: UserAgentItem[];
  /** Currently selected agent for chat (null = main agent) */
  currentAgent: UserAgentItem | null;
  /** Loading state */
  loading: boolean;
  /** Available MCP/skill resources for binding */
  availableResources: AvailableResources | null;

  fetchAgents: () => Promise<void>;
  fetchAvailableResources: () => Promise<void>;
  createAgent: (data: Partial<UserAgentItem>) => Promise<UserAgentItem>;
  updateAgent: (agentId: string, data: Partial<UserAgentItem>) => Promise<UserAgentItem>;
  deleteAgent: (agentId: string) => Promise<void>;
  setCurrentAgent: (agent: UserAgentItem | null) => void;
}

const getApiUrl = () => (import.meta.env.VITE_API_BASE_URL as string) || '/api';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function formatApiError(payload: unknown, status: number): string {
  const body = isRecord(payload) ? payload : {};
  const message = body.message;
  if (typeof message === 'string' && message.trim()) return message;

  const detail = body.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const firstMsg = detail.find(
      (item) => isRecord(item) && typeof item.msg === 'string',
    );
    if (isRecord(firstMsg) && typeof firstMsg.msg === 'string') {
      return firstMsg.msg;
    }
  } else if (isRecord(detail)) {
    if (typeof detail.message === 'string' && detail.message.trim()) return detail.message;
  }

  const errors = body.errors;
  if (Array.isArray(errors)) {
    const firstError = errors.find(
      (item) => isRecord(item) && typeof item.msg === 'string',
    );
    if (isRecord(firstError) && typeof firstError.msg === 'string') {
      const firstMsg = firstError.msg;
      if (firstMsg) return firstMsg;
    }
  }

  return `HTTP ${status}`;
}

async function agentApiRequest<T>(path: string, options?: RequestInit): Promise<T> {
  const url = `${getApiUrl()}${path}`;
  const response = await fetch(url, {
    ...options,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(options?.headers ?? {}),
    },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(formatApiError(payload, response.status));
  }
  const payload: unknown = await response.json();
  return (isRecord(payload) && 'data' in payload ? payload.data : payload) as T;
}

export const useAgentStore = create<AgentState>((set) => ({
  agents: [],
  currentAgent: null,
  loading: false,
  availableResources: null,

  fetchAgents: async () => {
    try {
      set({ loading: true });
      const data = await agentApiRequest<UserAgentItem[] | { items: UserAgentItem[] }>('/v1/agents');
      const items = Array.isArray(data) ? data : data.items;
      set({ agents: items, loading: false });
    } catch (e) {
      console.error('Failed to fetch agents:', e);
      set({ loading: false });
    }
  },

  fetchAvailableResources: async () => {
    try {
      const data = await agentApiRequest<AvailableResources>('/v1/agents/available-resources');
      set({ availableResources: data });
    } catch (e) {
      console.error('Failed to fetch available resources:', e);
    }
  },

  createAgent: async (data) => {
    const agent = await agentApiRequest<UserAgentItem>('/v1/agents', {
      method: 'POST',
      body: JSON.stringify(data),
    });
    set((state) => ({ agents: [...state.agents, agent] }));
    return agent;
  },

  updateAgent: async (agentId, data) => {
    const agent = await agentApiRequest<UserAgentItem>(`/v1/agents/${agentId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    });
    set((state) => ({
      agents: state.agents.map((a) => (a.agent_id === agentId ? agent : a)),
      currentAgent: state.currentAgent?.agent_id === agentId ? agent : state.currentAgent,
    }));
    return agent;
  },

  deleteAgent: async (agentId) => {
    await agentApiRequest<void>(`/v1/agents/${agentId}`, { method: 'DELETE' });
    set((state) => ({
      agents: state.agents.filter((a) => a.agent_id !== agentId),
      currentAgent: state.currentAgent?.agent_id === agentId ? null : state.currentAgent,
    }));
  },

  setCurrentAgent: (agent) => set({ currentAgent: agent }),
}));
