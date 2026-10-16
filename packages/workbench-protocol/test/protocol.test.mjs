import assert from 'node:assert/strict';
import { validateApplication } from '../dist/index.js';
const application = {
  schemaVersion: '1.0.0', id: 'reports', title: 'Reports',
  target: {kind: 'builtin', componentId: 'reports-page'},
};
assert.deepEqual(validateApplication(application, {componentIds: ['reports-page']}), []);
assert.ok(validateApplication(application, {componentIds: []}).some(e => e.path === '/target/componentId'));
console.log('application protocol: trusted builtin accepted; unknown component rejected');
const plugin = {...application, target: {kind: 'plugin', moduleId: 'main'}};
assert.deepEqual(validateApplication(plugin, {source: 'plugin', modules: [{id: 'main', surface: 'workspace'}]}), []);
assert.ok(validateApplication(plugin, {source: 'plugin', modules: [{id: 'main', surface: 'canvas'}]}).some(e => e.path === '/target/moduleId'));

import { readFile } from 'node:fs/promises';
import { HOST_CAPABILITIES } from '../dist/index.js';
const schema = JSON.parse(await readFile(new URL('../schemas/application.schema.json', import.meta.url)));
assert.deepEqual(schema.definitions.capabilities.items.enum, HOST_CAPABILITIES);
for (const capability of HOST_CAPABILITIES) {
  assert.deepEqual(validateApplication({...application, compatibility: {requiredCapabilities: [capability]}},
    {componentIds: ['reports-page']}), [], capability);
}
assert.ok(validateApplication({...application, compatibility: {requiredCapabilities: ['lifecycle.setDirty']}},
  {componentIds: ['reports-page'], capabilities: []}).some(e => e.code === 'unsupported_capability'));
