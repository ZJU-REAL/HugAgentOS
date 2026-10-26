import type { SessionUpdate } from '@agentclientprotocol/sdk';
import type { ChatEvent } from '../engine/engine.js';

/** One turn keeps stable tool identities without exporting internal backend event fields. */
export class Events {
  private known = new Set<string>();
  convert(e: ChatEvent): SessionUpdate[] {
    if(e.type==='plan_update' && Array.isArray(e.steps)) return [{sessionUpdate:'plan',entries:e.steps.flatMap(raw=>{
      if(!raw || typeof raw!=='object' || typeof raw.title!=='string') return [];
      return [{content:raw.title,priority:'medium' as const,status:raw.status==='completed'?'completed' as const:raw.status==='in_progress'?'in_progress' as const:'pending' as const}];
    })}];
    const usage=e.type==='context_usage'?e:e.type==='meta'?e.context_usage:undefined;
    if(usage && typeof usage==='object') {
      const u=usage as Record<string,unknown>;
      if(typeof u.used_tokens==='number' && typeof u.context_window==='number' && u.used_tokens>=0 && u.context_window>0)
        return [{sessionUpdate:'usage_update',used:u.used_tokens,size:u.context_window}];
    }
    if (e.type === 'content' || e.type === 'thinking') {
      const text = e.delta ?? e.content;
      return typeof text === 'string' && text ? [{ sessionUpdate: e.type === 'content' ? 'agent_message_chunk' : 'agent_thought_chunk',
        content: { type: 'text', text } }] : [];
    }
    if (['tool_call_start', 'tool_call', 'tool_result'].includes(e.type)) {
      const id = String(e.tool_call_id ?? e.call_id ?? e.tool_id ?? e.id ?? '');
      if (!id) return [];
      const title = String(e.name ?? e.tool_name ?? 'Tool');
      const status = e.type === 'tool_result' ? ['error','failed','interrupted','cancelled'].includes(String(e.status)) || e.is_error === true ? 'failed' : 'completed' : 'in_progress';
      const fields = { toolCallId: id, title, status,
        rawInput: e.tool_args ?? e.input ?? e.args ?? e.arguments,
        rawOutput: e.type === 'tool_result' ? e.result ?? e.output ?? e.summary : undefined } as const;
      if (!this.known.has(id)) { this.known.add(id); return [{ sessionUpdate: 'tool_call', ...fields }]; }
      return [{ sessionUpdate: 'tool_call_update', ...fields }];
    }
    return [];
  }
}
