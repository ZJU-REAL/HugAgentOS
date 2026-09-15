import assert from 'node:assert/strict';
import React from 'react';
import { message } from 'antd';
import { renderToString } from 'react-dom/server';

const memory = new Map<string, string>();
const storage = { getItem: (key: string) => memory.get(key) ?? null, setItem: (key: string, value: string) => { memory.set(key, value); }, removeItem: (key: string) => { memory.delete(key); } };
Object.assign(globalThis, { localStorage: storage, window: { localStorage: storage, sessionStorage: storage, setTimeout, clearTimeout, addEventListener() {}, removeEventListener() {} } });
Object.assign(globalThis, { document: { documentElement: {}, addEventListener() {}, removeEventListener() {} } });
const { useStreaming } = await import('../src/hooks/useStreaming');
const { useChatStore } = await import('../src/stores/chatStore');
const { useFileStore } = await import('../src/stores/fileStore');
const { setHybridDual } = await import('../src/api');
let actions!: ReturnType<typeof useStreaming>;
function Composer() { actions = useStreaming('/api', async () => {}, async () => {}); return null; }
renderToString(<Composer />);
setHybridDual(true);
const files = new Map<string, string>();
const sent: Array<Record<string, any>> = [];
let id = 0;
globalThis.fetch = async (input, init) => {
  const url = String(input);
  if (url.endsWith('/v1/file/upload')) {
    assert.equal(new Headers(init?.headers).get('x-hugagent-target'), 'local');
    const file = (init!.body as FormData).get('file') as File;
    const file_id = `ua_send_${++id}`;
    files.set(file_id, await file.text());
    return Response.json({ file_id, download_url: `/files/${file_id}`, name: file.name, size: file.size, mime_type: file.type });
  }
  if (url.endsWith('/v1/chats/stream') || url.endsWith('/v1/plans/generate')) {
    assert.equal(new Headers(init?.headers).get('x-hugagent-target'), 'local');
    const body = JSON.parse(String(init?.body));
    sent.push(body);
    assert.equal(files.get(body.attachments[0].file_id), 'attachment bytes');
    return new Response('data: [DONE]\n\n', { headers: { 'Content-Type': 'text/event-stream' } });
  }
  return Response.json({ code: 200, data: {} });
};
for (const planMode of [false, true]) {
  const chatId = planMode ? 'plan-attachment' : 'chat-attachment';
  useChatStore.setState({ currentChatId: chatId, planMode, loopMode: false, sending: false, input: 'Read the attachment', currentPlanId: null });
  useChatStore.getState().setChatRunTarget(chatId, 'local');
  const file = new File(['attachment bytes'], 'input.txt', { type: 'text/plain' });
  actions.handleFileSelect({ target: { files: [file] } } as unknown as React.ChangeEvent<HTMLInputElement>, { current: null });
  await actions.send();
  assert.equal(sent.length, planMode ? 2 : 1, 'the real send path must reach its chat/plan endpoint');
  const user = useChatStore.getState().store.chats[chatId].messages.find(message => message.role === 'user');
  assert.equal(user?.attachments?.[0].origin, 'local');
  assert.equal(files.get(user!.attachments![0].file_id!), 'attachment bytes');
  assert.equal(useFileStore.getState().uploadedFiles.length, 0);
}
console.log('PASS: actual composer selection and regular/plan send preserve local attachments and message ownership');

const successfulFetch = globalThis.fetch;
const errors: unknown[] = [];
const originalError = message.error;
message.error = ((content: unknown) => { errors.push(content); return () => {}; }) as typeof message.error;
try {
  for (const planMode of [false, true]) {
    const chatId = planMode ? 'plan-retry' : 'chat-retry';
    useChatStore.setState({ currentChatId: chatId, planMode, loopMode: false, sending: false, input: 'Keep this draft', currentPlanId: null });
    useChatStore.getState().setChatRunTarget(chatId, 'local');
    const file = new File(['attachment bytes'], 'retry.txt', { type: 'text/plain' });
    globalThis.fetch = async () => Response.json({ detail: 'temporary upload failure' }, { status: 503 });
    actions.handleFileSelect({ target: { files: [file] } } as unknown as React.ChangeEvent<HTMLInputElement>, { current: null });
    const before = sent.length;
    await actions.send();
    assert.equal(sent.length, before, 'upload failure must not start a model request');
    assert.equal(useChatStore.getState().input, 'Keep this draft');
    assert.deepEqual(useFileStore.getState().uploadedFiles, [file]);
    assert.equal(useChatStore.getState().sending, false);
    globalThis.fetch = successfulFetch;
    await actions.send();
    assert.equal(sent.length, before + 1, 'retry works through the actual send entry point');
    assert.equal(useFileStore.getState().uploadedFiles.length, 0);
  }
} finally { message.error = originalError; }
assert.ok(errors.length >= 2);
console.log('PASS: regular and plan upload failures preserve the draft and retry successfully');
