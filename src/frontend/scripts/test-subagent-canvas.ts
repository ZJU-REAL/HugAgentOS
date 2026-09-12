import assert from 'node:assert/strict';
import { useCanvasStore } from '../src/stores/canvasStore';

const store = useCanvasStore;
store.getState().resetSidebar();
const first = { chatId: 'chat-a', messageUid: 'message-a', toolId: 'call-a', agent: { agent_id: 'agent-a', name: 'Research', avatar: null } };
store.getState().openSubagent(first);
assert.equal(store.getState().activeView, 'subagent');
store.getState().openSubagent(first);
assert.equal(store.getState().tabs.length, 1, 'Reopening an invocation reuses its tab');
store.getState().openSubagent({ ...first, toolId: 'call-b' });
assert.equal(store.getState().tabs.length, 2, 'Repeated invocations of one agent remain separate');
store.getState().openOntology({ chatId: 'chat-a', messageUid: 'message-a' });
assert.equal(store.getState().tabs.length, 3, 'Subagent and ontology tabs coexist');
store.getState().activateTab(store.getState().tabs[0].id);
assert.equal(store.getState().subagentTarget?.toolId, 'call-a');
store.getState().closeCanvas();
store.getState().openSidebar();
assert.equal(store.getState().subagentTarget?.toolId, 'call-a');
store.getState().resetSidebar();
assert.equal(store.getState().subagentTarget, null, 'Changing chats clears the selected invocation');
assert.equal(store.getState().tabs.length, 0);
console.log('Subagent Canvas navigation passed');

const { subagentOutputText, subagentStatus } = await import('../src/utils/subagentView');
assert.equal(subagentOutputText({ content: [{ type: 'text', text: 'First' }, { type: 'text', text: 'Second' }] }), 'First\n\nSecond');
assert.equal(subagentOutputText('Plain reply'), 'Plain reply');
assert.equal(subagentOutputText({ answer: 'Structured reply' }), 'Structured reply');
assert.equal(subagentStatus({ status: 'running' }, false), 'interrupted');
assert.equal(subagentStatus({ status: 'running' }, true), 'running');
assert.equal(subagentStatus({ status: 'error' }, true), 'error');
assert.equal(subagentStatus({ status: 'success' }, true), 'success');
console.log('Subagent output and terminal status passed');

const { groupExecutionRuns } = await import('../src/utils/executionRuns');
const transcript = { uid: 'test', role: 'assistant' as const, content: '', ts: 0, isStreaming: true,
  toolCalls: [{ id: 't1', name: 'bash', status: 'running' as const }],
  segments: [{ type: 'thinking' as const, content: 'Reasoning' }, { type: 'tool' as const, toolIndex: 0 },
    { type: 'text' as const, content: 'Answer' }, { type: 'thinking' as const, content: 'Next phase' }] };
const visible = groupExecutionRuns(transcript, true);
assert.equal(visible.runs.length, 2, 'Answer text separates execution phases');
assert.deepEqual(visible.runs[0].steps.map(step => step.kind), ['thinking', 'tool']);
assert.equal(visible.suppressedIdx.has(2), false, 'Answer remains outside tool collapse');
const hidden = groupExecutionRuns(transcript, false);
assert.equal(hidden.runs[0].anchor, 1, 'Main UI off mode keeps leading thinking separate');
assert.deepEqual(hidden.runs[0].steps.map(step => step.kind), ['tool']);
console.log('Shared main/Canvas execution grouping passed');

const { subagentDisplayText } = await import('../src/utils/subagentView');
const heading = '【审查员】的回复：\n\n';
assert.equal(subagentDisplayText({ content: [{ type: 'text', text: heading + 'verdict: revise' }] }, '审查员'), 'verdict: revise');
assert.equal(subagentDisplayText(JSON.stringify({ content: [{ type: 'text', text: heading + 'History' }] }), '审查员'), 'History');
assert.equal(subagentDisplayText(heading + heading + 'Model text', '审查员'), heading + 'Model text', 'Strip only the outer framework heading');
assert.equal(subagentDisplayText('Quoted: ' + heading + 'Model text', '审查员'), 'Quoted: ' + heading + 'Model text');
assert.equal(subagentDisplayText(heading + 'Model text', '执行员'), heading + 'Model text', 'Do not strip another agent name');
assert.equal(subagentDisplayText('Plain reply', '审查员'), 'Plain reply');
console.log('Subagent framework heading presentation passed');
