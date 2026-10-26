import { RequestError, type AgentRequestContext, type PromptRequest, type PromptResponse } from '@agentclientprotocol/sdk';
import { cancelTurn, type Session } from './sessions.js';
import { promptContent } from './content.js';
import { Events } from './events.js';
import { elicit } from './elicitation.js';
import type { ClientCapabilities } from '@agentclientprotocol/sdk';
import { permission } from './permissions.js';

export async function prompt(s: Session, ctx: AgentRequestContext<PromptRequest>, capabilities: ClientCapabilities): Promise<PromptResponse> {
  if (s.closing || s.unavailable || s.configuring || s.replaying) throw RequestError.invalidRequest(undefined, 'Session is unavailable');
  if (s.active) throw RequestError.invalidRequest(undefined, 'Session already has an active prompt');
  let resolve!: () => void;
  let acknowledge!: (id?: string) => void;
  const transport = new AbortController();
  const startupTimer = setTimeout(() => transport.abort(), 30000);
  const controller = new AbortController();
  const active: NonNullable<Session['active']> = { controller, transport, started: new Promise(r => { acknowledge = r; }), runId: undefined as string | undefined,
    done: new Promise<void>(r => { resolve = r; }), resolve: () => resolve() };
  s.active = active;
  const cancel = () => { void cancelTurn(s).catch(() => {}); };
  ctx.signal.addEventListener('abort', cancel, { once: true });
  if (ctx.signal.aborted) cancel();
  const signal = controller.signal;
  const events = new Events();
  let failed = false;
  let ended = false;
  let submitted = false;
  let stopReason: PromptResponse['stopReason'] = 'end_turn';
  try {
    if (signal.aborted) return {stopReason: 'cancelled'};
    if(ctx.params.prompt.length===1 && ctx.params.prompt[0].type==='text' && ctx.params.prompt[0].text.trim()==='/compact') {
      const result=await s.engine.compact(s.id);
      await ctx.client.notify('session/update',{sessionId:s.id,update:{sessionUpdate:'agent_message_chunk',content:{type:'text',text:result.compacted?'Context compacted.':'No context required compaction.'}}});
      return {stopReason:signal.aborted?'cancelled':'end_turn'};
    }
    const {text,attachments}=await promptContent(s,ctx.params.prompt,signal);
    submitted=true;
    for await (const e of s.engine.send(s.id, text, { attachments, model: s.model, clientSessionId: s.leaseId, signal: transport.signal })) {
      if (e.type === 'run_started' && typeof e.run_id === 'string') { active.runId = e.run_id; clearTimeout(startupTimer); acknowledge(e.run_id); }
      if (signal.aborted) break;
      for (const update of events.convert(e)) await ctx.client.notify('session/update', { sessionId: s.id, update });
      if (e.type === 'user_question') await elicit(s, e, ctx.client, capabilities, signal);
      if (e.type === 'file_confirm') await permission(s, e, ctx.client, signal);
      if (e.type === 'error') failed = true;
      if (e.type === 'end') {
        ended = true;
        if (e.status === 'failed' || e.status === 'needs_attention') failed = true;
        if (e.status === 'cancelled') stopReason = 'cancelled';
        if (['max_tokens', 'max_turn_requests', 'refusal'].includes(String(e.stop_reason)))
          stopReason = e.stop_reason as PromptResponse['stopReason'];
      }
    }
    if (signal.aborted) return { stopReason: 'cancelled' };
    if (!ended && active.runId) {
      const terminal = await s.engine.runStatus(active.runId);
      ended = terminal.status === 'completed';
      if (terminal.status === 'cancelled') return {stopReason:'cancelled'};
    }
    if (!ended || failed) throw RequestError.internalError(undefined, 'Engine run failed');
    return { stopReason };
  } catch (error) {
    const cancelled=signal.aborted;
    if (submitted && !ended) {
      acknowledge(active.runId);
      if (active.runId) await cancelTurn(s);

    }
    if (cancelled) return { stopReason: 'cancelled' };
    throw error;
  } finally {
    clearTimeout(startupTimer);
    controller.abort();
    acknowledge();
    ctx.signal.removeEventListener('abort', cancel);
    try {
      // A clean stream close is not proof that an unacknowledged submission never ran.
      if (submitted && !active.runId) {
        s.unavailable = true;
        clearInterval(s.timer);
        await s.engine.releaseClientSession(s.id, s.leaseId);
      }
      await active.cancelling;
    }
    finally { s.active = undefined; active.resolve(); }
  }
}
