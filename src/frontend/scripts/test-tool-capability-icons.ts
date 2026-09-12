import assert from 'node:assert/strict';
import { resolveToolCapability } from '../src/utils/toolCapability';
const catalog = { skills: [{ id: 'writer', name: 'Writing', icon: '/skill.svg' }], mcp: [{ id: 'search', name: 'Search', icon: '/search.svg', tools: ['search_web'] }] };
const plugins = [{ slug: 'office', install_id: 'install-office', name: 'Office', icon: '/office.svg', skills: ['plugin-writer'], mcp: ['office-mcp'], tools: ['office_tool'] }];
assert.equal(resolveToolCapability({ name: 'load_skill', input: { skill_name: 'writer' } }, catalog, plugins)?.icon, '/skill.svg');
assert.equal(resolveToolCapability({ name: 'load_plugin', input: { plugin: 'office' } }, catalog, plugins)?.icon, '/office.svg');
assert.equal(resolveToolCapability({ name: 'search_web' }, catalog, plugins)?.kind, 'connector');
assert.equal(resolveToolCapability({ name: 'bash' }, catalog, plugins), null, 'Ordinary tools retain their category icon');
assert.equal(resolveToolCapability({ name: 'search_web' }, { ...catalog, mcp: [...catalog.mcp, { id: 'other', name: 'Other', tools: ['search_web'] }] }, plugins), null, 'Ambiguous tool ownership must not pick an arbitrary logo');
assert.equal(resolveToolCapability({ name: 'load_skill', input: { skill_name: 'missing' } }, catalog, plugins)?.id, 'missing', 'Unknown skill uses stable fallback');
assert.equal(resolveToolCapability({ name: 'load_plugin', input: { plugin: 'install-office' } }, catalog, plugins)?.icon, '/office.svg');
assert.equal(resolveToolCapability({ name: 'office_tool' }, catalog, plugins, { slug: 'office', icon: '/tool.svg' })?.icon, '/office.svg');
console.log('Tool capability icon resolution passed');

assert.equal(resolveToolCapability({ name: 'load_skill', input: { file_path: '/workspace/skills/writer/SKILL.md' } }, catalog, plugins)?.icon, '/skill.svg');
assert.equal(resolveToolCapability({ name: 'view_text_file', input: { file_path: '/workspace/skills/writer/SKILL.md' } }, catalog, plugins)?.icon, '/skill.svg');
assert.equal(resolveToolCapability({ name: 'view_text_file', input: { file_path: '/workspace/skills/writer/README.md' } }, catalog, plugins), null);
assert.equal(resolveToolCapability({ name: 'view_text_file', input: { file_path: '/tmp/unknown/SKILL.md' } }, catalog, plugins), null);
assert.equal(resolveToolCapability({ name: 'load_skill', output: JSON.stringify({ skill_id: 'writer' }) }, catalog, plugins)?.icon, '/skill.svg');

assert.equal(resolveToolCapability({ name: 'load_skill', input: { skill_name: 'plugin-writer' } }, catalog, plugins)?.icon, '/office.svg');
assert.equal(resolveToolCapability({ name: 'view_text_file', input: { file_path: '/skills/plugin-writer/SKILL.md' } }, catalog, plugins)?.icon, '/office.svg');
assert.equal(resolveToolCapability({ name: 'office_tool' }, catalog, plugins)?.icon, '/office.svg');
assert.equal(resolveToolCapability({ name: 'search_web' }, catalog, plugins)?.icon, '/search.svg');
