import assert from 'node:assert/strict';
import { createWorkbenchRegistry } from '../src/workbench/registry';
assert.throws(() => createWorkbenchRegistry({
  components: {}, commands: {},
  applications: [{ id: 'missing', target: { kind: 'builtin', componentId: 'untrusted/path' } }],
}), /Unknown component/, 'unknown component is rejected before an entry becomes available');
console.log('workbench registry: unknown component rejected');

import { renderToStaticMarkup } from 'react-dom/server';
import { WorkbenchPage } from '../src/workbench/WorkbenchPage';
const page = { id: 'reports', target: { kind: 'builtin' as const, componentId: 'reports-page' } };
assert.throws(() => createWorkbenchRegistry({components: {'reports-page': () => null}, commands: {}, applications:[page,page]}), /Duplicate/, 'duplicate declarations cannot replace a registered page');
assert.throws(() => createWorkbenchRegistry({components: {}, commands: {}, applications:[{id:'bad',target:{kind:'command',commandId:'shell'}}]}), /Unknown command/, 'only registered commands can execute');
const registry = createWorkbenchRegistry<{ title: string }>({
  components: {'reports-page': ({title}) => <h1>{title}</h1>}, commands: {}, applications:[page],
});
assert.match(renderToStaticMarkup(<WorkbenchPage registry={registry} applicationId="reports" context={{title:'Report overview'}} fallback={null}/>), /Report overview/, 'injected page receives its host context');
assert.throws(() => registry.component('unknown'), /Unknown application/);
console.log('workbench registry: duplicate ID, command whitelist, page context checks passed');
