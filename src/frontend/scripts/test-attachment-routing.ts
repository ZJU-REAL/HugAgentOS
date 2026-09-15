import assert from 'node:assert/strict';
import { setHybridDual, registerLocalChat, setChatRoutingContext, registerLocalProject, onUnauthorized, uploadFile, authFetch, chatTargetHeaders } from '../src/api';
import { prepareChatAttachments } from '../src/utils/chatAttachments';
import { uploadFileToOSS } from '../src/utils/fileParser';

// Two independent backends: only the endpoint that received the upload can read it.
const stored = { local: new Map<string, string>(), cloud: new Map<string, string>() };
const requests: Array<{ url: string; target: 'local' | 'cloud' }> = [];
let nextId = 0;
globalThis.fetch = async (input, init) => {
  const url = new URL(String(input), 'http://desktop.invalid');
  const target = new Headers(init?.headers).get('x-hugagent-target') === 'local'
    || url.searchParams.get('hg_target') === 'local' ? 'local' : 'cloud';
  requests.push({ url: url.pathname, target });
  if (init?.method === 'POST' && url.pathname.endsWith('/v1/file/upload')) {
    const file = (init.body as FormData).get('file') as File;
    const file_id = `ua_fixture_${++nextId}`;
    stored[target].set(file_id, await file.text());
    return Response.json({ file_id, download_url: `/files/${file_id}`, name: file.name, mime_type: file.type, size: file.size });
  }
  const id = url.pathname.split('/').at(-1)!;
  const content = stored[target].get(id);
  return content === undefined ? Response.json({ detail: `File not found: ${id}` }, { status: 404 }) : new Response(content);
};
setHybridDual(true);
registerLocalChat('local-chat');
for (const [name, type] of [['image.png', 'image/png'], ['notes.txt', 'text/plain'], ['report.pdf', 'application/pdf'], ['table.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet']]) {
  for (const upload of [uploadFileToOSS, (file: File, _base: string, chat: string) => uploadFile(file, chat)]) {
    const file = new File(['fixture bytes'], name, { type });
    const result = await upload(file, '/api', 'local-chat');
    const response = await authFetch(`/api/files/${result.file_id}`, { headers: chatTargetHeaders('local-chat') });
    assert.equal(response.status, 200, `${name}: uploaded attachment must be readable on the chat backend`);
    assert.equal(await response.text(), 'fixture bytes');
  }
}
console.log('PASS: image and document uploads are readable on the local chat backend');

let target = 'cloud';
setChatRoutingContext(() => ({ runTarget: target }));
const switchedFile = new File(['switched content'], 'switch.txt', { type: 'text/plain' });
const original = uploadFileToOSS(switchedFile, '/api', 'draft-chat');
await original;
target = 'local';
const prepared = await prepareChatAttachments([switchedFile], new Map([[switchedFile, original]]), [], '/api', 'draft-chat');
const switchedRead = await authFetch(`/api/files/${prepared[0].file_id}`, { headers: chatTargetHeaders('draft-chat') });
assert.equal(switchedRead.status, 200, 'switching the draft target must not send an ID from the previous backend');
assert.equal(await switchedRead.text(), 'switched content');
console.log('PASS: upload followed by target switch remains readable');

// My Space is cloud-owned in dual mode; attaching it to a local task needs bytes, not its cloud ID.
target = 'cloud';
const cloudFile = await uploadFile(new File(['space content'], 'space.txt', { type: 'text/plain' }), 'cloud-draft');
target = 'local';
const imported = { ...cloudFile, type: 'document' as const };
const spaceAttachments = await prepareChatAttachments([], new Map(), [imported], '/api', 'draft-chat');
const spaceRead = await authFetch(`/api/files/${spaceAttachments[0].file_id}`, { headers: chatTargetHeaders('draft-chat') });
assert.equal(spaceRead.status, 200, 'cloud My Space references must be materialized on the local execution backend');
assert.equal(await spaceRead.text(), 'space content');
assert.equal(stored.cloud.get(cloudFile.file_id), 'space content', 'the source file must be preserved');
console.log('PASS: My Space references follow the selected task backend');

registerLocalProject('bound-local-project');
target = 'cloud';
requests.length = 0;
const projectUpload = await uploadFileToOSS(new File(['project content'], 'project.txt'), '/api', 'unbound-draft', 'bound-local-project');
assert.equal(requests[0].target, 'local', 'project composer uploads must go local even before the draft binding is persisted');
assert.equal(stored.local.get(projectUpload.file_id), 'project content');
console.log('PASS: project composer routes the initial upload correctly');

const backendFetch = globalThis.fetch;
target = 'local';
const retryFile = new File(['retry content'], 'retry.txt');
const retryUploads = new Map();
globalThis.fetch = async () => Response.json({ detail: 'temporary failure' }, { status: 503 });
await assert.rejects(prepareChatAttachments([retryFile], retryUploads, [], '/api', 'draft-chat'));
globalThis.fetch = backendFetch;
const retried = await prepareChatAttachments([retryFile], retryUploads, [], '/api', 'draft-chat');
assert.equal(stored.local.get(retried[0].file_id), 'retry content', 'a failed upload must be retryable without removing the attachment');
console.log('PASS: failed uploads can be retried without losing the draft');

// A route change while the transfer is pending must not submit an old-end ID.
let release!: () => void;
const paused = new Promise<void>((resolve) => { release = resolve; });
const slowFile = new File(['slow'], 'slow.txt');
globalThis.fetch = async (input, init) => { await paused; return backendFetch(input, init); };
const inFlight = prepareChatAttachments([slowFile], new Map(), [], '/api', 'draft-chat');
await new Promise(resolve => setTimeout(resolve, 0));
target = 'cloud';
release();
await assert.rejects(inFlight, /运行位置已变化/);
globalThis.fetch = backendFetch;

// Preview ownership is frozen at upload time, even if the draft now points elsewhere.
const { useDeploymentModeStore } = await import('../src/stores/deploymentModeStore');
const { artifactUrl } = await import('../src/utils/artifactAccess');
useDeploymentModeStore.setState({ isDesktop: true, provisionMode: 'dual', activeLocal: false });
setHybridDual(true);
assert.match(artifactUrl({ ...prepared[0], chat_id: 'draft-chat' }), /hg_target=local/);
assert.doesNotMatch(artifactUrl({ ...cloudFile, chat_id: 'local-chat' }), /hg_target/);
console.log('PASS: in-flight target changes are rejected; previews keep the actual file origin');

// Missing/forbidden referenced resources stop attachment assembly; never omit the file silently.
target = 'local';
for (const status of [403, 404]) {
  globalThis.fetch = async () => Response.json({ detail: 'unavailable' }, { status });
  await assert.rejects(prepareChatAttachments([], new Map(), [imported], '/api', 'draft-chat'), /无法读取引用文件/);
}
globalThis.fetch = backendFetch;

// Local upload authentication errors do not invalidate the independent cloud session.
let loginPrompts = 0;
onUnauthorized(() => { loginPrompts++; });
globalThis.fetch = async () => Response.json({ detail: 'login required' }, { status: 401 });
await assert.rejects(uploadFile(new File(['x'], 'x.txt'), 'local-chat'));
assert.equal(loginPrompts, 0);
target = 'cloud';
await assert.rejects(uploadFile(new File(['x'], 'x.txt'), 'cloud-draft'));
assert.equal(loginPrompts, 1);
globalThis.fetch = backendFetch;

// Cloud My Space and cloud chats remain cloud; single-backend deployments need no routing marker.
requests.length = 0;
await uploadFile(new File(['cloud'], 'cloud.txt'), 'cloud-draft');
await uploadFile(new File(['space'], 'space.txt'), undefined, 'cloud-folder');
assert.ok(requests.every(request => request.target === 'cloud'));
setHybridDual(false);
requests.length = 0;
await uploadFileToOSS(new File(['single'], 'single.txt'), '/custom/api', 'local-chat');
assert.deepEqual(requests, [{ url: '/custom/api/v1/file/upload', target: 'cloud' }]);
console.log('PASS: missing-file, permission, authentication, cloud, and single-backend controls');

// A generated cloud resource can exceed upload limits even though the picker accepts it.
setHybridDual(true);
target = 'local';
let chunksRead = 0;
let cancelled = false;
globalThis.fetch = async () => new Response(new ReadableStream({
  pull(controller) {
    chunksRead++;
    if (chunksRead > 100) controller.close();
    else controller.enqueue(new Uint8Array(1024 * 1024));
  },
  cancel() { cancelled = true; },
}));
await assert.rejects(prepareChatAttachments([], new Map(), [imported], '/api', 'draft-chat'), /文件过大/);
assert.ok(cancelled && chunksRead < 100, 'oversized downloads must stop before consuming the entire response');
globalThis.fetch = backendFetch;
console.log('PASS: oversized imported files are cancelled during download');
