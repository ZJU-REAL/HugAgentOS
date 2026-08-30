export interface ChatInvocationContext {
  skill?: { id: string; name: string };
  plugin?: { name: string; skillIds: string[]; mcpIds: string[] };
  connector?: { id: string; name: string };
  mention?: { id: string; name: string };
}

export interface ChatInvocationSource {
  activeSkill?: { id: string; name: string } | null;
  activePlugin?: { name: string; skillIds: string[]; mcpIds: string[] } | null;
  activeConnector?: { id: string; name: string } | null;
  activeMention?: { id: string; name: string } | null;
}

export interface QueuedChatTurnSnapshot {
  id: string;
  content: string;
  createdAt: number;
  status: 'queued';
  invocation: ChatInvocationContext;
}

const cleanString = (value: unknown): string => (
  typeof value === 'string' ? value.trim().slice(0, 512) : ''
);

const cleanIds = (value: unknown): string[] => {
  if (!Array.isArray(value)) return [];
  return Array.from(new Set(value.map(cleanString).filter(Boolean))).slice(0, 100);
};

/** Snapshot composer capability chips before clearing the contentEditable input. */
export function captureChatInvocation(source: ChatInvocationSource): ChatInvocationContext {
  const skillId = cleanString(source.activeSkill?.id);
  const skillName = cleanString(source.activeSkill?.name);
  const pluginName = cleanString(source.activePlugin?.name);
  const connectorId = cleanString(source.activeConnector?.id);
  const connectorName = cleanString(source.activeConnector?.name);
  const mentionId = cleanString(source.activeMention?.id);
  const mentionName = cleanString(source.activeMention?.name);
  const invocation: ChatInvocationContext = {};

  if (skillId && skillName) invocation.skill = { id: skillId, name: skillName };
  if (pluginName) {
    invocation.plugin = {
      name: pluginName,
      skillIds: cleanIds(source.activePlugin?.skillIds),
      mcpIds: cleanIds(source.activePlugin?.mcpIds),
    };
  }
  if (connectorId && connectorName) {
    invocation.connector = { id: connectorId, name: connectorName };
  }
  if (mentionId && mentionName) invocation.mention = { id: mentionId, name: mentionName };
  return invocation;
}

/** Build the storage-safe queued turn before the composer resets its chip state. */
export function createQueuedChatTurn(
  input: {
    id: string;
    content: string;
    createdAt: number;
    source: ChatInvocationSource;
    invocationOverride?: ChatInvocationContext;
  },
): QueuedChatTurnSnapshot {
  return {
    id: input.id,
    content: input.content,
    createdAt: input.createdAt,
    status: 'queued',
    invocation: input.invocationOverride === undefined
      ? captureChatInvocation(input.source)
      : normalizeChatInvocation(input.invocationOverride),
  };
}

/** Treat localStorage as untrusted and rebuild only the supported invocation shape. */
export function normalizeChatInvocation(value: unknown): ChatInvocationContext {
  if (!value || typeof value !== 'object') return {};
  const candidate = value as Record<string, unknown>;
  const skill = candidate.skill && typeof candidate.skill === 'object'
    ? candidate.skill as Record<string, unknown>
    : undefined;
  const plugin = candidate.plugin && typeof candidate.plugin === 'object'
    ? candidate.plugin as Record<string, unknown>
    : undefined;
  const connector = candidate.connector && typeof candidate.connector === 'object'
    ? candidate.connector as Record<string, unknown>
    : undefined;
  const mention = candidate.mention && typeof candidate.mention === 'object'
    ? candidate.mention as Record<string, unknown>
    : undefined;
  return captureChatInvocation({
    activeSkill: skill ? { id: cleanString(skill.id), name: cleanString(skill.name) } : null,
    activePlugin: plugin ? {
      name: cleanString(plugin.name),
      skillIds: cleanIds(plugin.skillIds),
      mcpIds: cleanIds(plugin.mcpIds),
    } : null,
    activeConnector: connector
      ? { id: cleanString(connector.id), name: cleanString(connector.name) }
      : null,
    activeMention: mention
      ? { id: cleanString(mention.id), name: cleanString(mention.name) }
      : null,
  });
}

/** Recover a queued turn after JSON/localStorage round-tripping. */
export function queuedChatInvocation(value: unknown): ChatInvocationContext {
  if (!value || typeof value !== 'object') return {};
  return normalizeChatInvocation((value as Record<string, unknown>).invocation);
}

export function hasChatInvocation(invocation?: ChatInvocationContext): boolean {
  return !!(
    invocation?.skill
    || invocation?.plugin
    || invocation?.connector
    || invocation?.mention
  );
}

/** Fields shared by the ordinary /v1/chats/stream request contract. */
export function chatInvocationRequestFields(invocation: ChatInvocationContext) {
  const pluginMcpIds = invocation.plugin?.mcpIds ?? [];
  const mcpIds = Array.from(new Set([
    ...pluginMcpIds,
    ...(invocation.connector ? [invocation.connector.id] : []),
  ]));
  return {
    ...(invocation.skill ? {
      skill_id: invocation.skill.id,
      skill_name: invocation.skill.name,
    } : {}),
    ...(invocation.plugin?.skillIds.length ? { skill_ids: invocation.plugin.skillIds } : {}),
    ...(mcpIds.length ? { mcp_ids: mcpIds } : {}),
    ...(invocation.plugin ? { plugin_name: invocation.plugin.name } : {}),
    ...(invocation.connector ? {
      connector_id: invocation.connector.id,
      connector_name: invocation.connector.name,
    } : {}),
    ...(invocation.mention ? {
      mention_agent_id: invocation.mention.id,
      mention_name: invocation.mention.name,
    } : {}),
  };
}

/** Badge fields shared by optimistic user bubbles and queued-turn projections. */
export function chatInvocationMessageProps(invocation: ChatInvocationContext) {
  return {
    ...(invocation.skill ? {
      skillId: invocation.skill.id,
      skillName: invocation.skill.name,
    } : {}),
    ...(invocation.plugin ? { pluginName: invocation.plugin.name } : {}),
    ...(invocation.connector ? { connectorName: invocation.connector.name } : {}),
    ...(invocation.mention ? { mentionName: invocation.mention.name } : {}),
  };
}
