import assert from 'node:assert/strict';
import {
  setHybridDual, registerLocalChat, registerLocalProject, chatTargetHeaders, isRegisteredLocalChat, listLoops, getLoopIterations,
  generatePlanStream, executePlanStream, getPlanApi, updatePlanApi, cancelPlanApi,
  createLoop, startLoop, resumeLoop, getLoop, steerLoop, cancelLoop, getSession,
} from '../src/api';
import { useChatStore } from '../src/stores/chatStore';

const requests: Array<{ url: string; init?: RequestInit }> = [];
globalThis.fetch = async (url, init) => {
  requests.push({ url: String(url), init });
  return new Response(JSON.stringify({ code: 200, data: { loop_id: 'loop-local' } }), { status: 200 });
};
setHybridDual(true);
// Exercise the same public store action as the desktop selector, before any send.
useChatStore.getState().setChatRunTarget('selected-chat', 'local');
assert.equal(chatTargetHeaders('selected-chat')['x-hugagent-target'], 'local');
assert.equal(isRegisteredLocalChat('selected-chat'), false, 'selection alone must not lock an empty draft');
useChatStore.getState().setChatRunTarget('selected-chat', undefined);
assert.deepEqual(chatTargetHeaders('selected-chat'), {}, 'an empty draft can switch back to cloud');
useChatStore.getState().setChatRunTarget('selected-chat', 'local');
useChatStore.getState().bindChatProject('selected-chat', 'cloud-project', 'Cloud');
assert.deepEqual(chatTargetHeaders('selected-chat'), {}, 'cloud project binding supersedes a draft local choice');
useChatStore.getState().unbindChatProject('selected-chat');
useChatStore.getState().setChatRunTarget('selected-chat', 'local');
await generatePlanStream('test', 'qwen', undefined, [], [], [], 'selected-chat');
await getPlanApi('plan-local', 'selected-chat');
await updatePlanApi('plan-local', { status: 'approved' }, 'selected-chat');
await executePlanStream('plan-local', undefined, [], [], [], 'selected-chat');
await cancelPlanApi('plan-local', 'selected-chat');
await createLoop({ goal_spec: { objective: 'test' }, chat_id: 'selected-chat' });
await startLoop('loop-local', {}, undefined, 'selected-chat');
await getLoopIterations('loop-local');
await startLoop('loop-local');
await cancelLoop('loop-local');
await resumeLoop('loop-local', {}, undefined, 'selected-chat');
await getLoop('loop-local', 'selected-chat');
await steerLoop('loop-local', 'continue', 'selected-chat');
await cancelLoop('loop-local', 'selected-chat');
// A restored persisted local chat routes before the local history list is ready.
const selected = useChatStore.getState().store.chats['selected-chat'];
useChatStore.getState().updateStore((store) => ({ ...store,
  chats: { ...store.chats, 'selected-chat': { ...selected, projectId: 'not-yet-loaded-project', runTarget: 'local' } },
}));

await getSession('selected-chat');
for (const request of requests) {
  assert.equal(new Headers(request.init?.headers).get('x-hugagent-target'), 'local', request.url);
}
requests.length = 0;
registerLocalChat('listed-local-chat');
assert.equal(isRegisteredLocalChat('listed-local-chat'), true, 'listed history stays locked before its messages load');
await generatePlanStream('test', 'qwen', undefined, [], [], [], 'listed-local-chat');
registerLocalProject('local-project');
await createLoop({ goal_spec: { objective: 'test' }, project_id: 'local-project' });
await generatePlanStream('test', 'qwen', undefined, [], [], [], undefined, [], [], [], 'local-project');
for (const request of requests) {
  assert.equal(new Headers(request.init?.headers).get('x-hugagent-target'), 'local', request.url);
}
requests.length = 0;
globalThis.fetch = async (url, init) => {
  requests.push({ url: String(url), init });
  const local = new Headers(init?.headers).get('x-hugagent-target') === 'local';
  return new Response(JSON.stringify({ code: 200, data: [{ loop_id: local ? 'restored-loop' : 'cloud-loop', chat_id: local ? 'restored-chat' : 'cloud-chat' }] }));
};
const listed = await listLoops();
assert.equal(listed.length, 2, 'the desktop panel includes local and cloud loops');
requests.length = 0;
await startLoop('restored-loop');
await cancelLoop('restored-loop');
await getLoopIterations('restored-loop');
for (const request of requests) {
  assert.equal(new Headers(request.init?.headers).get('x-hugagent-target'), 'local', request.url);
}
requests.length = 0;
await generatePlanStream('cloud', 'qwen', undefined, [], [], [], 'cloud-chat');
await createLoop({ goal_spec: { objective: 'cloud' }, chat_id: 'cloud-chat' });
setHybridDual(false);
await generatePlanStream('web', 'qwen', undefined, [], [], [], 'selected-chat');
for (const request of requests) {
  assert.equal(new Headers(request.init?.headers).get('x-hugagent-target'), null, request.url);
}
console.log('desktop task routing: selector, restore, plan lifecycle, loop lifecycle, project and cloud controls passed');
