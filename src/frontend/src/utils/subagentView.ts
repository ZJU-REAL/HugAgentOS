import type { ToolCall } from '../types';

export function subagentOutputText(value: unknown): string {
  if (typeof value === 'string') {
    try { return subagentOutputText(JSON.parse(value)); } catch { return value; }
  }
  if (Array.isArray(value)) return value.map(subagentOutputText).filter(Boolean).join('\n\n');
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    for (const key of ['text', 'content', 'answer', 'result', 'output']) {
      if (record[key] !== undefined) return subagentOutputText(record[key]);
    }
    return JSON.stringify(value, null, 2);
  }
  return value == null ? '' : String(value);
}

/** Hide the call_subagent transport heading only at the Canvas boundary. */
export function subagentDisplayText(value: unknown, agentName: string): string {
  const text = subagentOutputText(value);
  const heading = `【${agentName}】的回复：\n\n`;
  return text.startsWith(heading) ? text.slice(heading.length) : text;
}

export function subagentStatus(tool: Pick<ToolCall, 'status'>, isStreaming?: boolean) {
  if (tool.status === 'error' || tool.status === 'interrupted') return tool.status;
  if (tool.status === 'running' || tool.status === 'pending') return isStreaming ? 'running' : 'interrupted';
  return 'success';
}
