import assert from 'node:assert/strict';
import {validatePluginContributions} from '../src/workbench/pluginContributions';
const good = {schemaVersion: '1.0.0', id: 'reports', title: 'Reports', target: {kind: 'plugin', moduleId: 'main'}};
const item = {slug: 'reports', version: 3, contributes: {
  modules: [{id: 'main', entry: 'web/main.html', surface: 'workspace', title: 'Reports', grants: []}],
  applications: [good, {...good, id: 'broken', target: {kind: 'plugin', moduleId: 'missing'}}],
}};
assert.deepEqual(validatePluginContributions(item).item?.contributes.applications?.map(a => a.id), ['reports']);
assert.equal(validatePluginContributions({...item, version: 99}).item, null);
const legacy = {slug: 'old', version: 2, contributes: {tool_views: [{tools: ['t'], view: 'list'}]}};
assert.deepEqual(validatePluginContributions(legacy).item, legacy);
console.log('browser boundary: invalid app isolated, unknown UI version refused, old UI retained');
const memory = new Map<string, string>();
const storage = {getItem: (key: string) => memory.get(key) ?? null, setItem: (key: string, value: string) => memory.set(key, value)};
Object.assign(globalThis, {document: {documentElement: {}, addEventListener() {}}, localStorage: storage, window: {localStorage: storage, location: {origin: 'https://host.test'}, addEventListener() {}, removeEventListener() {}}});
globalThis.fetch = async () => Response.json({code: 10000, data: {items: [
  item, {...item, slug: 'future', version: 999},
  {...item, slug: 'damaged', contributes: {modules: [{id: 'unsafe', surface: 'workspace', entry: 'web/main.html', icon: {secret: 'SECRET'}}, {id: 'bad-tools', surface: 'workspace', entry: 'web/main.html', for_tools: 17}]}},
  legacy,
]}});
const {usePluginUiStore} = await import('../src/stores/pluginUiStore');
await usePluginUiStore.getState().fetchContributions(true);
assert.deepEqual(usePluginUiStore.getState().items.map(p => p.slug), ['reports', 'damaged', 'old']);
assert.deepEqual(usePluginUiStore.getState().items[1].contributes.modules, []);
assert.equal(usePluginUiStore.getState().findToolView('t')?.slug, 'old');
console.log('HTTP directory boundary: damaged plugin cannot suppress healthy plugins or legacy tool rendering');
assert.ok(usePluginUiStore.getState().diagnostics.some(e => e.path === '/items/0/contributes/applications/1/target/moduleId'));
assert.ok(!JSON.stringify(usePluginUiStore.getState().diagnostics).includes('SECRET'));
