import assert from 'node:assert/strict';
import React from 'react';
import { renderToString } from 'react-dom/server';
import { MemoryRouter } from 'react-router';
import type { ChatItem } from '../src/types';
import { buildAutomationSidebarEntries } from '../src/components/automation/automationSidebarEntries';
import { AutomationSidebar } from '../src/components/automation/AutomationSidebar';
import { useChatStore } from '../src/stores/chatStore';
import type { SidebarProps } from '../src/components/sidebar/sidebarTypes';

const chat = (id: string, taskId: string | undefined, createdAt: number): ChatItem => ({
  id, automationTaskId: taskId, automationRun: true, title: 'Daily report',
  createdAt, updatedAt: createdAt, messages: [],
});
const oldest = { ...chat('old', 'task', 1), pinned: true, favorite: true };
const latest = chat('latest', 'task', 3);
const input = [latest, oldest, chat('middle', 'task', 2)];
const entries = buildAutomationSidebarEntries(input);
assert.equal(entries.length, 1, 'one task has one sidebar record');
assert.equal(entries[0].item.id, 'old', 'new runs preserve the entry identity and preferences');
assert.equal(entries[0].targetChatId, 'latest', 'the entry opens the newest execution');
assert.equal(entries[0].item.updatedAt, 3);
assert.equal(entries[0].item.pinned, true);
assert.equal(entries[0].item.favorite, true);
assert.equal(input.length, 3, 'grouping keeps all run conversations');
assert.equal(oldest.updatedAt, 1, 'grouping never mutates stored chats');

useChatStore.setState({ store: {
  chats: Object.fromEntries(input.map(item => [item.id, item])),
  order: input.map(item => item.id),
}, chatsLoading: false });
Object.assign(useChatStore.getInitialState(), useChatStore.getState());
const noop = () => {};
const actions: SidebarProps = {
  onNewChat: noop, onNewProjectChat: noop, onDeleteChat: noop,
  onTogglePinned: noop, onToggleFavorite: noop, onStartRename: noop,
  onCommitRename: noop, onExportChat: noop, onSelectChat: noop, onSetPanel: noop,
};
for (const path of ['/automation/task/conversations/old', '/automation/task/conversations/latest', '/automation/task']) {
  const markup = renderToString(<MemoryRouter initialEntries={[path]}><AutomationSidebar actions={actions} /></MemoryRouter>);
  assert.equal((markup.match(/class="jx-historyTitle"/g) || []).length, 1);
  assert.equal((markup.match(/class="jx-historyItem active/g) || []).length, 1, 'switching runs keeps the task selected');
}
console.log('PASS: one sidebar entry per task, stable identity, newest execution and historical active state');

const setup = (id: string): ChatItem => ({ ...chat(id, 'new', 5), messages: [{ role: 'user', content: 'Create report', ts: 5 }] });
const separate = buildAutomationSidebarEntries([
  ...input, chat('same-title', 'other-task', 4), chat('local-run', 'local:task', 5),
  setup('setup-a'), setup('setup-b'), chat('empty-setup', 'new', 6),
  chat('legacy-a', undefined, 7), chat('legacy-b', undefined, 8),
  { ...chat('ordinary', undefined, 9), automationRun: false },
]);
assert.deepEqual(separate.map(entry => entry.item.id), ['old', 'same-title', 'local-run', 'setup-a', 'setup-b', 'legacy-a', 'legacy-b']);
const appended = buildAutomationSidebarEntries([...input, chat('next-run', 'task', 10)]);
assert.equal(appended[0].item.id, 'old');
assert.equal(appended[0].targetChatId, 'next-run');
const editedOldRun = buildAutomationSidebarEntries([{ ...oldest, updatedAt: 100 }, latest]);
assert.equal(editedOldRun[0].targetChatId, 'latest', 'editing an older run does not change the newest execution');

const { useAutomationChatStore } = await import('../src/stores/automationChatStore');
const { bindRouter } = await import('../src/routing/navigation');
let destination = '';
bindRouter({ navigate: path => { destination = path; } });
useAutomationChatStore.setState({
  activeGroup: { taskId: 'task', taskName: 'Daily report', runs: [
    { run_id: 'run-latest', task_id: 'task', status: 'success', chat_id: 'latest', started_at: '2026-10-13T10:00:00Z' },
    { run_id: 'run-old', task_id: 'task', status: 'success', chat_id: 'old', started_at: '2026-10-12T10:00:00Z' },
  ] },
});
// Run selection publishes the current route even in this server-rendered fixture.
Object.assign(globalThis, { window: { location: { pathname: '/automation/task/conversations/latest', search: '' } } });
useAutomationChatStore.getState().selectRun('run-old');
Reflect.deleteProperty(globalThis, 'window');
assert.equal(destination, '/automation/task/conversations/old');
assert.equal(useAutomationChatStore.getState().selectedRunId, 'run-old');
assert.equal(buildAutomationSidebarEntries(Object.values(useChatStore.getState().store.chats)).length, 1);
console.log('PASS: distinct tasks, hybrid IDs, setup chats and legacy runs remain separate; right panel selection retains one task');

useChatStore.setState({ remoteRunningChatIds: new Set(['latest']) });
Object.assign(useChatStore.getInitialState(), useChatStore.getState());
const runningMarkup = renderToString(<MemoryRouter initialEntries={['/automation/task/conversations/latest']}><AutomationSidebar actions={actions} /></MemoryRouter>);
assert.match(runningMarkup, /data-task-status="running"/, 'the grouped task shows the newest running execution');
