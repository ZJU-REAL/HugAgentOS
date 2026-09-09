import assert from 'node:assert/strict';
import { setHybridDual, createAutomation, listAutomations, pauseAutomation, getAutomationRuns } from '../src/api';

const requests: Array<{url: string; init?: RequestInit}> = [];
globalThis.fetch = async (url, init) => {
  requests.push({url: String(url), init});
  const local = new Headers(init?.headers).get('x-hugagent-target') === 'local';
  const data = init?.method === 'POST'
    ? {task_id: 'same-id', execution_location: local ? 'local' : 'cloud'}
    : String(url).includes('/runs') ? [{run_id: 'run', task_id: 'same-id', chat_id: 'local-chat'}]
    : [{task_id: 'same-id', execution_location: local ? 'local' : 'cloud'}];
  return new Response(JSON.stringify({code: 200, data}));
};
setHybridDual(true);
const local = await createAutomation({task_type: 'prompt', prompt: 'report', cron_expression: '0 18 * * *',
  execution_location: 'local', project_id: 'local-project'});
assert.equal(new Headers(requests.at(-1)?.init?.headers).get('x-hugagent-target'), 'local');
const all = await listAutomations();
assert.equal(all.length, 2);
assert.notEqual(all[0].task_id, all[1].task_id, 'two backends may return identical IDs');
await pauseAutomation(local.task_id);
assert.equal(new Headers(requests.at(-1)?.init?.headers).get('x-hugagent-target'), 'local');
await getAutomationRuns(local.task_id);
assert.equal(new Headers(requests.at(-1)?.init?.headers).get('x-hugagent-target'), 'local');
assert(!requests.at(-1)?.url.includes('local%3A'), 'UI namespace never reaches database lookup');
console.log('automation execution routing passed');


const { defaultExecutionLocation, executionLocationError } = await import('../src/components/lab/automationLocation');
assert.equal(defaultExecutionLocation('dual', 'local'), 'local');
assert.equal(defaultExecutionLocation('dual'), 'cloud');
assert.equal(defaultExecutionLocation('local_only'), 'local');
assert(executionLocationError('cloud', 'local', 'report'));
assert(executionLocationError('cloud', undefined, 'Read C:\\Users\\Aaron'));
assert(executionLocationError('local', undefined, 'Read C:\\Users\\Aaron'));
assert.equal(executionLocationError('local', 'local', 'Read project'), null);
const { renderToStaticMarkup } = await import('react-dom/server');
const { createElement } = await import('react');
const { AutomationCard } = await import('../src/components/lab/AutomationCard');
const markup = renderToStaticMarkup(createElement(AutomationCard, { task: {
  task_id: 'local:one', task_type: 'prompt', execution_location: 'local',
  device_name: 'Laptop', project_name: 'Reports', timezone: 'Asia/Shanghai', status: 'active',
  cron_expression: '0 18 * * *', schedule_type: 'recurring', run_count: 0,
} as never, onClick: () => {} }));
assert(markup.includes('Laptop') && markup.includes('Reports') && markup.includes('Asia/Shanghai'));
console.log('automation defaults, conflict validation and rendered location labels passed');

globalThis.fetch = async (url, init) => {
  const local = new Headers(init?.headers).get('x-hugagent-target') === 'local';
  if (!local) throw new Error('cloud offline');
  return new Response(JSON.stringify({code: 200, data: [{task_id: 'available-local'}]}));
};
const partial = await listAutomations();
assert.equal(partial.length, 1);
assert.equal(partial[0].task_id, 'local:available-local');
const { automationAvailabilityWarning } = await import('../src/api');
assert(automationAvailabilityWarning().includes('云端'));
console.log('offline cloud preserves local task list with explicit warning');

assert.equal(executionLocationError('cloud', undefined, 'Summarize https://example.com/news'), null);
