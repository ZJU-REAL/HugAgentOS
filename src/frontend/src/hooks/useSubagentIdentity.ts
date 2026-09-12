import { useEffect } from 'react';
import { t } from '../i18n';
import { useAgentStore } from '../stores/agentStore';
import type { ToolCall } from '../types';

export function useSubagentIdentity(tool: ToolCall) {
  useEffect(() => {
    const state = useAgentStore.getState();
    // Multiple visible rows share the store's synchronous loading flag.
    if (!state.agents.length && !state.loading) void state.fetchAgents();
  }, []);
  return useAgentStore((state) => {
    const id = typeof tool.input?.agent_id === 'string' ? tool.input.agent_id : '';
    return state.agents.find((agent) => id ? agent.agent_id === id : agent.name === tool.subagentName);
  }) || { agent_id: tool.input?.agent_id || '', name: tool.subagentName || t('子智能体'), avatar: null };
}
