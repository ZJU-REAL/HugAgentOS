import type { Catalog, ChatMessage, ToolCall } from '../../types';
import type { CanvasArtifact } from '../../stores/canvasStore';
import { artifactOrigin } from '../../utils/artifactAccess';
import { extractArtifactOutputs } from '../../utils/fileParser';
import type { ToolCapability } from '../../utils/toolCapability';

export interface MonitorSubagent {
  tool: ToolCall;
  messageUid: string;
  isStreaming: boolean;
}

interface MonitorFiles {
  input: CanvasArtifact[];
  output: CanvasArtifact[];
}

interface MonitorToolStats {
  calls: number;
  failed: number;
}

/** Every call_subagent in the chat, in the order the agent made them. */
export function collectSubagents(messages: ChatMessage[] | undefined): MonitorSubagent[] {
  const result: MonitorSubagent[] = [];
  for (const m of messages || []) {
    if (m.role !== 'assistant') continue;
    for (const tool of m.toolCalls || []) {
      if (tool.name === 'call_subagent' && tool.id) {
        result.push({ tool, messageUid: m.uid, isStreaming: !!m.isStreaming });
      }
    }
  }
  return result;
}

function toArtifact(raw: Record<string, unknown>, chatId?: string): CanvasArtifact {
  const name = String(raw.name || raw.filename || '').split(/[\\/]/).pop() || String(raw.file_id);
  const artifact = {
    ...raw,
    file_id: String(raw.file_id),
    name,
    url: typeof raw.url === 'string' ? raw.url : '',
    chat_id: typeof raw.chat_id === 'string' ? raw.chat_id : chatId,
  } as CanvasArtifact;
  return { ...artifact, origin: artifactOrigin(artifact) };
}

/**
 * Files that went into and came out of the chat.
 *
 * Input is what the user attached. Output is every file a tool or sub-agent
 * wrote, including intermediates the agent did not pin to the conversation:
 * the monitor shows the whole run, not only the delivered cards.
 */
export function collectFiles(messages: ChatMessage[] | undefined, chatId?: string): MonitorFiles {
  const input = new Map<string, CanvasArtifact>();
  const output = new Map<string, CanvasArtifact>();
  const collect = (value: unknown) => {
    for (const raw of extractArtifactOutputs(value)) {
      const artifact = toArtifact(raw, chatId);
      output.delete(artifact.file_id);
      output.set(artifact.file_id, artifact);
    }
  };
  for (const m of messages || []) {
    if (m.role === 'user') {
      for (const att of m.attachments || []) {
        if (!att.file_id) continue;
        input.set(att.file_id, toArtifact({
          file_id: att.file_id, origin: att.origin, name: att.name,
          url: att.download_url || `/files/${att.file_id}`, mime_type: att.mime_type,
        }, chatId));
      }
      continue;
    }
    for (const tool of m.toolCalls || []) {
      if (tool.status != null && tool.status !== 'success') continue;
      // pin_to_workspace echoes a file that another tool already produced.
      if (tool.name !== 'pin_to_workspace') collect(tool.output);
      for (const step of tool.subSteps || []) {
        if (step.kind === 'tool' && step.status === 'success') collect(step.output);
      }
    }
  }
  return { input: [...input.values()], output: [...output.values()] };
}

/** Tool calls made in the chat, sub-agent steps included. */
export function countToolCalls(messages: ChatMessage[] | undefined): MonitorToolStats {
  let calls = 0;
  let failed = 0;
  for (const m of messages || []) {
    for (const tool of m.toolCalls || []) {
      calls += 1;
      if (tool.status === 'error') failed += 1;
      for (const step of tool.subSteps || []) {
        if (step.kind !== 'tool') continue;
        calls += 1;
        if (step.status === 'error') failed += 1;
      }
    }
  }
  return { calls, failed };
}

/** A skill, plugin or connector the latest turn ran with. */
interface TurnCapability extends ToolCapability {
  /** The user picked it in the composer (/skill, /plugin, MCP chip) for this turn. */
  explicit: boolean;
}

type ToolRef = Pick<ToolCall, 'name' | 'input' | 'output'>;

interface CapabilityLookup {
  /** The shared tool → capability resolver (useToolCapabilityResolver). */
  resolveTool: (tool: ToolRef) => ToolCapability | null;
  connectors: Pick<Catalog, 'mcp'>['mcp'];
}

/** Resolve each tool object once: finished tool calls keep their identity across streamed updates. */
export function cachePerTool(resolve: CapabilityLookup['resolveTool']): CapabilityLookup['resolveTool'] {
  const resolved = new WeakMap<object, ToolCapability | null>();
  return (tool) => {
    let hit = resolved.get(tool);
    if (hit === undefined) {
      hit = resolve(tool);
      resolved.set(tool, hit);
    }
    return hit;
  };
}

/**
 * Skills, plugins and connectors bound to the chat's latest turn: the ones the
 * user picked for it first, then the ones the agent loaded or called while
 * answering (sub-agent calls included). Each appears once.
 */
export function collectTurnCapabilities(messages: ChatMessage[] | undefined, lookup: CapabilityLookup): TurnCapability[] {
  const list = messages || [];
  let start = -1;
  for (let i = list.length - 1; i >= 0; i -= 1) {
    if (list[i].role === 'user') { start = i; break; }
  }
  const result = new Map<string, TurnCapability>();
  const add = (capability: ToolCapability | null, explicit: boolean) => {
    if (!capability?.name) return;
    const key = `${capability.kind}:${capability.id || capability.name}`;
    if (!result.has(key)) result.set(key, { ...capability, explicit });
  };
  /** A pick resolved like the agent's own load_skill / load_plugin call, so both land on one key. */
  const addPicked = (tool: ToolRef, token: string, name: string) => {
    const capability = lookup.resolveTool(tool);
    // Not in the catalog: keep the name the user saw rather than a raw id.
    add(capability && capability.name === token ? { ...capability, name } : capability, true);
  };

  const user = start >= 0 ? list[start] : undefined;
  if (user?.skillName) {
    const token = user.skillId || user.skillName;
    addPicked({ name: 'load_skill', input: { skill_name: token } }, token, user.skillName);
  }
  if (user?.pluginName) {
    const token = user.pluginId || user.pluginName;
    addPicked({ name: 'load_plugin', input: { plugin: token } }, token, user.pluginName);
  }
  if (user?.connectorName) {
    // Older messages carry only the name the composer showed, sometimes with an "MCP" suffix.
    const bareName = user.connectorName.replace(/\s*MCP$/i, '');
    const connector = lookup.connectors.find((item) => item.id === user.connectorId)
      ?? lookup.connectors.find((item) => item.name === user.connectorName || item.name === bareName);
    add({ kind: 'connector', id: connector?.id || user.connectorId || user.connectorName, name: connector?.name || user.connectorName, icon: connector?.icon }, true);
  }

  for (const m of list.slice(start + 1)) {
    if (m.role !== 'assistant') continue;
    for (const tool of m.toolCalls || []) {
      add(lookup.resolveTool(tool), false);
      for (const step of tool.subSteps || []) {
        if (step.kind === 'tool' && step.name) add(lookup.resolveTool(step as ToolRef), false);
      }
    }
  }
  return [...result.values()];
}
