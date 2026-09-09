import assert from 'node:assert/strict';
import { setHybridDual, listSites, prepareLocalSiteProject, openLocalSiteEditor,
  isLocalProject, isRegisteredLocalChat } from '../src/api';

const requests: Array<{url: string; target: string | null}> = [];
globalThis.fetch = async (url, init) => {
  const path = String(url);
  const target = new Headers(init?.headers).get('x-hugagent-target');
  requests.push({url: path, target});
  let data: unknown;
  if (path.endsWith('/prepare')) data = { project_id: 'local-project', project_name: 'Site', source_dir: '/project/site' };
  else if (path.endsWith('/edit')) data = { site_id: 'site-1', project_id: 'local-project', project_name: 'Site', chat_id: 'original-chat', source_dir: '/project/site' };
  else if (path.endsWith('/local/site-sources')) data = { items: [
    { site_id: 'site-1', project_id: 'local-project', chat_id: 'original-chat', source_dir: '/project/site' },
    { site_id: 'site-view-only', project_id: 'local-project', chat_id: 'view-chat' },
  ] };
  else data = { items: [
    {site_id: 'site-1', project_id: null, editable: false, permission: 'admin'},
    {site_id: 'site-2', project_id: null, editable: false, permission: 'admin'},
    {site_id: 'site-view-only', project_id: null, editable: false, permission: 'view'},
  ] };
  return new Response(JSON.stringify({code: 200, data}));
};
setHybridDual(true);
await prepareLocalSiteProject();
assert.equal(isLocalProject('local-project'), true);
const {items} = await listSites();
assert.equal(items[0].editable, true);
assert.equal(items[0].project_id, null, 'cloud project identity is not overwritten');
assert.equal(items[0].local_source?.project_id, 'local-project');
assert.equal(items[1].editable, false);
assert.equal(items[2].editable, false, 'view permission cannot acquire edit from a local binding');
const edit = await openLocalSiteEditor('site-1');
assert.equal(edit.chat_id, 'original-chat');
assert.equal(isRegisteredLocalChat('original-chat'), true);
for (const req of requests) {
  assert.equal(req.target, req.url.includes('/local/site-sources') ? 'local' : null);
}
requests.length = 0;
setHybridDual(false);
await listSites();
assert.equal(requests.length, 1, 'web does not probe desktop sources');
console.log('Local site routing and editing passed');
