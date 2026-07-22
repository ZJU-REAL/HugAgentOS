import { create } from 'zustand';

import type { OntologyTagOption } from '../types';
import { createApiResponseError } from '../utils/apiError';

export interface AgentChangeHistoryItem {
  version: string;
  timestamp: string;
  content: string;
  operator_name: string;
  details: Array<{ field: string; before: string; after: string }>;
}

export interface UserAgentItem {
  agent_id: string;
  owner_type: 'admin' | 'user';
  user_id: string | null;
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
  ontology_tags: string[];
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
  ontology_tags: OntologyTagOption[];
}

interface AgentState {
  agents: UserAgentItem[];
  currentAgent: UserAgentItem | null;
  loading: boolean;
  availableResources: AvailableResources | null;
  fetchAgents: () => Promise<void>;
  fetchAvailableResources: () => Promise<void>;
  createAgent: (data: Partial<UserAgentItem>) => Promise<UserAgentItem>;
  updateAgent: (agentId: string, data: Partial<UserAgentItem>) => Promise<UserAgentItem>;
  deleteAgent: (agentId: string) => Promise<void>;
  setCurrentAgent: (agent: UserAgentItem | null) => void;
}

const apiBase = () => (import.meta.env.VITE_API_BASE_URL as string) || '/api';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBase()}${path}`, {
    ...options,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(options?.headers ?? {}) },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw createApiResponseError(response.status, payload, `HTTP ${response.status}`);
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
    set({ loading: true });
    try {
      const data = await request<UserAgentItem[] | { items: UserAgentItem[] }>('/v1/agents');
      set({ agents: Array.isArray(data) ? data : data.items });
    } catch (error) {
      console.error('Failed to fetch agents:', error);
    } finally {
      set({ loading: false });
    }
  },

  fetchAvailableResources: async () => {
    try {
      set({ availableResources: await request<AvailableResources>('/v1/agents/available-resources') });
    } catch (error) {
      console.error('Failed to fetch available resources:', error);
    }
  },

  createAgent: async (data) => {
    const agent = await request<UserAgentItem>('/v1/agents', { method: 'POST', body: JSON.stringify(data) });
    set((state) => ({ agents: [...state.agents, agent] }));
    return agent;
  },

  updateAgent: async (agentId, data) => {
    const agent = await request<UserAgentItem>(`/v1/agents/${agentId}`, { method: 'PUT', body: JSON.stringify(data) });
    set((state) => ({
      agents: state.agents.map((item) => item.agent_id === agentId ? agent : item),
      currentAgent: state.currentAgent?.agent_id === agentId ? agent : state.currentAgent,
    }));
    return agent;
  },

  deleteAgent: async (agentId) => {
    await request<void>(`/v1/agents/${agentId}`, { method: 'DELETE' });
    set((state) => ({
      agents: state.agents.filter((item) => item.agent_id !== agentId),
      currentAgent: state.currentAgent?.agent_id === agentId ? null : state.currentAgent,
    }));
  },

  setCurrentAgent: (currentAgent) => set({ currentAgent }),
}));
