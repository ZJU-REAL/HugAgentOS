import assert from 'node:assert/strict';
const storage = {getItem: () => null, setItem() {}, removeItem() {}};
Object.assign(globalThis, {localStorage: storage, window: {
  location: {pathname: '/', origin: 'http://localhost'}, localStorage: storage,
  sessionStorage: storage, setTimeout, clearTimeout, addEventListener() {}, removeEventListener() {},
}, document: {documentElement: {}, addEventListener() {}, removeEventListener() {}}});
const {useChatStore} = await import('../src/stores/chatStore');
const {syncConversationTitle} = await import('../src/hooks/chatTitle');
const {createStreamSend} = await import('../src/hooks/streamSend');
const {processChatStream} = await import('../src/hooks/chatStream');
const chat = (id: string) => ({id, title: '新对话', messages: [], createdAt: 1, updatedAt: 1});
useChatStore.setState({currentChatId: 'first', store: {chats: {first: chat('first')}, order: ['first']}, sending: false});
let stream!: ReadableStreamDefaultController<Uint8Array>;
let titleRead!: () => void;
const reading = new Promise<void>(resolve => {titleRead = resolve;});
globalThis.fetch = async input => {
  const url = String(input);
  if (url.endsWith('/v1/agents/responses')) return new Response(new ReadableStream({start(controller) {stream = controller;}}));
  if (url.endsWith('/v1/chats/first')) {
    titleRead();
    return Response.json({data: {title: '季度销售分析', metadata: {title_generation: {status: 'completed'}}}});
  }
  return Response.json({data: {}});
};
const sender = createStreamSend({effectiveApiUrl: '/api', abortControllersRef: {current: new Map()},
  followUpAbortRef: {current: new Map()}, generateSummary: id => syncConversationTitle('/api', id),
  generateClassification: async () => {}, settleQueuedMessageAfterRun: () => {},
  processChatStreamWithHandoffRecovery: (response, id, options) => processChatStream(response, {chatId: id, ...options}),
});
const sending = sender.send('请分析本季度销售数据');
await reading;
for (let i = 0; i < 10 && useChatStore.getState().store.chats.first.title !== '季度销售分析'; i++) await new Promise(resolve => setTimeout(resolve, 5));
assert.equal(useChatStore.getState().store.chats.first.title, '季度销售分析', 'temporary seed must not block model title');
assert.ok(useChatStore.getState().sendingChatIds.has('first'), 'title updates before the answer ends');
stream.enqueue(new TextEncoder().encode('data: [DONE]\n\n'));stream.close();await sending;

for (const change of ['rename', 'delete', 'switch']) {
  const id = 'race-' + change;
  useChatStore.getState().updateStore(prev => ({...prev, chats: {...prev.chats, [id]: chat(id)}}));
  let release!: (response: Response) => void;
  globalThis.fetch = async () => new Promise<Response>(resolve => {release = resolve;});
  const work = syncConversationTitle('/api', id);
  if (change === 'rename') useChatStore.getState().updateStore(prev => ({...prev, chats: {...prev.chats, [id]: {...prev.chats[id], title: '我的标题', titleManuallySet: true}}}));
  if (change === 'delete') useChatStore.getState().updateStore(prev => {const chats = {...prev.chats};delete chats[id];return {...prev, chats};});
  if (change === 'switch') useChatStore.setState({currentChatId: 'first'});
  release(Response.json({data: {title: '异步结果', metadata: {title_generation: {status: 'completed'}}}}));
  await work;
  const result = useChatStore.getState().store.chats[id];
  if (change === 'rename') assert.equal(result.title, '我的标题');
  if (change === 'delete') assert.equal(result, undefined);
  if (change === 'switch') {
    assert.equal(result.title, '异步结果');
    assert.equal(useChatStore.getState().store.chats.first.title, '季度销售分析');
  }
}
const failed = chat('failed');
useChatStore.getState().updateStore(prev => ({...prev, chats: {...prev.chats, failed}}));
globalThis.fetch = async () => Response.json({data: {title: 'unused', metadata: {title_generation: {status: 'failed'}}}});
await syncConversationTitle('/api', 'failed');
assert.equal(useChatStore.getState().store.chats.failed.title, '新对话');
console.log('PASS title: real send updates before stream ends; seed replacement; in-flight rename; deletion; chat switch; failed model keeps fallback');

useChatStore.getState().updateStore(prev => ({...prev, chats: {...prev.chats, delayed: chat('delayed')}}));
let reads = 0;
globalThis.fetch = async () => Response.json({data: {title: ++reads === 1 ? '兜底' : '后台标题', metadata: {title_generation: {status: reads === 1 ? 'pending' : 'completed'}}}});
const delayed = syncConversationTitle('/api', 'delayed');
assert.equal(syncConversationTitle('/api', 'delayed'), delayed, 'duplicate followers share one request chain');
await delayed;
assert.equal(reads, 2);
assert.equal(useChatStore.getState().store.chats.delayed.title, '后台标题');
console.log('PASS title: pending-to-completed polling and follower deduplication');
