import { chatDraftKey, readComposer } from '../src/stores/composerStore';
import assert from 'node:assert/strict';
import React from 'react';
import { message } from 'antd';
import { renderToString } from 'react-dom/server';

const memory = new Map<string, string>();
const storage = { getItem: (key: string) => memory.get(key) ?? null, setItem: (key: string, value: string) => { memory.set(key, value); }, removeItem: (key: string) => { memory.delete(key); } };
Object.assign(globalThis, { localStorage: storage, window: { location: { pathname: '/', origin: 'http://localhost' }, localStorage: storage, sessionStorage: storage, setTimeout, clearTimeout, addEventListener() {}, removeEventListener() {} } });
Object.assign(globalThis, { document: { documentElement: {}, addEventListener() {}, removeEventListener() {} } });
const { useStreaming } = await import('../src/hooks/useStreaming');
const { useChatStore } = await import('../src/stores/chatStore');

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
  if (url.endsWith('/v1/agents/responses') || url.endsWith('/v1/plans/generate')) {
    assert.equal(new Headers(init?.headers).get('x-hugagent-target'), 'local');
    const body = JSON.parse(String(init?.body));
    if (url.endsWith('/v1/agents/responses')) assert.equal(body.stream, true);
    sent.push(body);
    assert.equal(files.get(body.attachments[0].file_id), 'attachment bytes');
    return new Response('data: [DONE]\n\n', { headers: { 'Content-Type': 'text/event-stream' } });
  }
  return Response.json({ code: 200, data: {} });
};
for (const planMode of [false, true]) {
  const chatId = planMode ? 'plan-attachment' : 'chat-attachment';
  useChatStore.setState({ currentChatId: chatId, planMode, loopMode: false, sending: false,  currentPlanId: null }); readComposer().setInput('Read the attachment');
  useChatStore.getState().setChatRunTarget(chatId, 'local');
  const file = new File(['attachment bytes'], 'input.txt', { type: 'text/plain' });
  actions.handleFileSelect({ target: { files: [file] } } as unknown as React.ChangeEvent<HTMLInputElement>, { current: null });
  await actions.send();
  assert.equal(sent.length, planMode ? 2 : 1, 'the real send path must reach its chat/plan endpoint');
  const user = useChatStore.getState().store.chats[chatId].messages.find(message => message.role === 'user');
  assert.equal(user?.attachments?.[0].origin, 'local');
  assert.equal(files.get(user!.attachments![0].file_id!), 'attachment bytes');
  assert.equal(readComposer().uploadedFiles.length, 0);
}
console.log('PASS: actual composer selection and regular/plan send preserve local attachments and message ownership');

const successfulFetch = globalThis.fetch;
const errors: unknown[] = [];
const originalError = message.error;
message.error = ((content: unknown) => { errors.push(content); return () => {}; }) as typeof message.error;
try {
  for (const planMode of [false, true]) {
    const chatId = planMode ? 'plan-retry' : 'chat-retry';
    useChatStore.setState({ currentChatId: chatId, planMode, loopMode: false, sending: false,  currentPlanId: null }); readComposer().setInput('Keep this draft');
    useChatStore.getState().setChatRunTarget(chatId, 'local');
    const file = new File(['attachment bytes'], 'retry.txt', { type: 'text/plain' });
    globalThis.fetch = async () => Response.json({ detail: 'temporary upload failure' }, { status: 503 });
    actions.handleFileSelect({ target: { files: [file] } } as unknown as React.ChangeEvent<HTMLInputElement>, { current: null });
    const before = sent.length;
    await actions.send();
    assert.equal(sent.length, before, 'upload failure must not start a model request');
    assert.equal(readComposer().input, 'Keep this draft');
    assert.deepEqual(readComposer().uploadedFiles, [file]);
    assert.equal(useChatStore.getState().sending, false);
    globalThis.fetch = successfulFetch;
    await actions.send();
    assert.equal(sent.length, before + 1, 'retry works through the actual send entry point');
    assert.equal(readComposer().uploadedFiles.length, 0);
  }
} finally { message.error = originalError; }
assert.ok(errors.length >= 2);
console.log('PASS: regular and plan upload failures preserve the draft and retry successfully');

// Waiting for A's upload while editing B must preserve B and A's sending configuration.
useChatStore.setState({currentChatId:'slow-a',sending:false,planMode:false,loopMode:false,modeSlug:'mode-a',chatMode:'high'});
useChatStore.getState().setChatRunTarget('slow-a','local');
const slow = new File(['attachment bytes'],'slow.txt',{type:'text/plain'});
let releaseUpload!: () => void;
const gate = new Promise<void>(resolve => {releaseUpload=resolve;});
globalThis.fetch = async (input,init) => {
  if(String(input).endsWith('/v1/file/upload')) await gate;
  return successfulFetch(input,init);
};
readComposer().setInput('A question');
actions.handleFileSelect({target:{files:[slow]}} as unknown as React.ChangeEvent<HTMLInputElement>,{current:null});
const sendingA=actions.send();
readComposer(chatDraftKey('slow-a')).setInput('A next question');
useChatStore.setState({currentChatId:'editing-b',sending:false,modeSlug:'mode-b',chatMode:'fast'});
readComposer().setInput('B question');
releaseUpload();
await sendingA;
assert.equal(readComposer().input,'B question');
assert.equal(readComposer(chatDraftKey('slow-a')).input,'A next question');
assert.equal(useChatStore.getState().store.chats['slow-a'].modeSlug,'mode-a');
assert.equal(useChatStore.getState().store.chats['slow-a'].thinkingEffort,'high');
assert.equal(sent.at(-1)?.mode_slug,'mode-a');

// Queuing captures text/capabilities; unsupported attachment/reference fields stay in the draft.
useChatStore.setState({currentChatId:'queue-owner',sending:true,planMode:false,loopMode:false,queuedMessages:{}});
const retained = new File(['later'],'later.txt');
readComposer().setInput('Next turn');
readComposer().setUploadedFiles([retained]);
readComposer().setQuotedFollowUp({text:'Quoted question',ts:1});
readComposer().setActivePlugin({id:'p',name:'Plugin'});
await actions.send();
assert.equal(useChatStore.getState().queuedMessages['queue-owner'].content,'Next turn');
assert.equal(readComposer().input,'');
assert.deepEqual(readComposer().uploadedFiles,[retained]);
assert.equal(readComposer().quotedFollowUp?.text,'Quoted question');
assert.equal(readComposer().activePlugin,null);
console.log('PASS: async send preserves draft owners/configuration/edits, queue consumes only its payload');

// Loop mode sends the objective only, leaving unrelated composer fields available.
const { sendLoopMode } = await import('../src/hooks/useLoopMode');
useChatStore.setState({currentChatId:'loop-owner',sending:false,loopMode:true});
readComposer().setInput('Loop objective');
readComposer().setUploadedFiles([retained]);
readComposer().setQuotedFollowUp({text:'Keep quote',ts:2});
readComposer().setActivePlugin({id:'keep-plugin',name:'Keep plugin'});
globalThis.fetch=async (input) => String(input).endsWith('/start')
  ? new Response('data: [DONE]\n\n',{headers:{'Content-Type':'text/event-stream'}})
  : Response.json({code:200,data:{loop_id:'loop-fixture'}});
await sendLoopMode({current:new Map()});
assert.equal(readComposer().input,'');
assert.deepEqual(readComposer().uploadedFiles,[retained]);
assert.equal(readComposer().quotedFollowUp?.text,'Keep quote');
assert.equal(readComposer().activePlugin?.id,'keep-plugin');
console.log('PASS: loop consumes only its objective and retains unsent fields');

useChatStore.setState({currentChatId:'replay-owner',sending:false,loopMode:false,planMode:false});
useChatStore.getState().setChatRunTarget('replay-owner','local');
readComposer().setInput('Next unsent turn');
readComposer().setActivePlugin({id:'next-plugin',name:'Next plugin'});
readComposer().setActiveCommand({id:'init',label:'初始化',command:'/init'});
readComposer().setUploadedFiles([retained]);
globalThis.fetch=async (input,init) => {
  if(String(input).endsWith('/v1/agents/responses')){
    const payload=JSON.parse(String(init?.body));
    assert.equal(payload.plugin_id,'queued-plugin');
    assert.equal(payload.attachments?.length ?? 0,0);
    return new Response('data: [DONE]\n\n',{headers:{'Content-Type':'text/event-stream'}});
  }
  return Response.json({code:200,data:{}});
};
await actions.send('Queued text',{plugin:{id:'queued-plugin',name:'Queued plugin'},skill:null,connector:null,mention:null});
assert.equal(readComposer().input,'Next unsent turn');
assert.equal(readComposer().activePlugin?.id,'next-plugin');
assert.equal(readComposer().activeCommand?.command,'/init');
assert.deepEqual(readComposer().uploadedFiles,[retained]);
console.log('PASS: replayed queue leaves the next draft and its capabilities/attachments intact');
