import type { AgentContext, ClientCapabilities, ElicitationSchema } from '@agentclientprotocol/sdk';
import type { ChatEvent, QuestionAnswer } from '../engine/engine.js';
import type { Session } from './sessions.js';
import { abortable } from './permissions.js';

export async function elicit(s: Session, event: ChatEvent, client: AgentContext,
  capabilities: ClientCapabilities, signal: AbortSignal) {
  if (typeof event.request_id !== 'string' || !Array.isArray(event.questions)) return;
  const questions = event.questions as {id: string; question?: string; options?: {id: string; label: string}[]; multi_select?: boolean}[];
  const answers: QuestionAnswer[] = questions.map(q => ({id: q.id, selected: [], skipped: true}));
  if (capabilities.elicitation?.form) {
    const properties: NonNullable<ElicitationSchema['properties']> = Object.create(null);
    for (const q of questions) {
      properties[q.id] = {type: 'string', title: q.question ?? q.id,
        description: q.options?.map(o => `${o.id}: ${o.label}`).join('; ')};
    }
    const response = await abortable(client.request('elicitation/create', {mode: 'form', sessionId: s.id,
      message: 'The agent needs your input. For multiple choices, enter option IDs separated by commas.',
      requestedSchema: {type: 'object', properties}}, {cancellationSignal: signal}), signal);
    if (response.action === 'accept' && response.content && typeof response.content === 'object') questions.forEach((q, i) => {
      const value = (response.content as Record<string, unknown>)[q.id];
      if (typeof value !== 'string' || !value.trim()) return;
      const ids = value.split(',').map(x => x.trim());
      const selected = ids.filter(id => q.options?.some(o => o.id === id));
      answers[i] = selected.length === ids.length ? {id:q.id, selected} : {id:q.id, selected:[], custom:value};
    });
  } else {
    await client.notify('session/update', {sessionId:s.id, update:{sessionUpdate:'agent_message_chunk',
      content:{type:'text',text:'Client does not support form elicitation; the question was skipped.'}}});
  }
  if (!signal.aborted) await s.engine.answer(s.id, event.request_id, answers);
}
