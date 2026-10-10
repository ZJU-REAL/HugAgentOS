import assert from 'node:assert/strict';
import { createWeixinBindingFlow, type WeixinBindingState } from '../src/components/settings/weixinBindingFlow';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
const timers = new Map<number, { run: () => void; delay: number }>();
let nextTimer = 0;
globalThis.setTimeout = ((run: () => void, delay: number) => {
  timers.set(++nextTimer, { run, delay });
  return nextTimer;
}) as unknown as typeof setTimeout;
globalThis.clearTimeout = ((id: number) => timers.delete(id)) as unknown as typeof clearTimeout;
const flush = async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve(); };
const tick = async (delay: number) => {
  const found = [...timers].find(([, item]) => item.delay === delay);
  assert(found, `timer ${delay} missing`);
  timers.delete(found[0]); found[1].run(); await flush();
};
let states: WeixinBindingState[] = [];
let confirmed = 0;
let polls = 0;
const pending = deferred<{status: string}>();
const flow = createWeixinBindingFlow({
  start: async () => ({ bind_id: 'bind', qrcode_img: 'png' }),
  poll: async () => { polls += 1; return pending.promise; },
  onChange: state => states.push(state),
  onConfirmed: async () => { confirmed += 1; },
});
await flow.start();
assert.equal(states.at(-1)?.phase, 'waiting');
await tick(2000);
assert.equal(polls, 1);
assert(![...timers.values()].some(item => item.delay === 2000), 'no overlapping long-poll request');
flow.close();
pending.resolve({status:'confirmed'}); await flush();
assert.equal(confirmed, 0, 'closed attempts cannot report success');
assert.equal(states.at(-1)?.open, false);
assert.equal(timers.size, 0);

states = [];
const first = deferred<{bind_id: string; qrcode_img: string}>();
let starts = 0;
const retry = createWeixinBindingFlow({
  start: async () => ++starts === 1 ? first.promise : {bind_id:'new',qrcode_img:'new-png'},
  poll: async id => { assert.equal(id,'new'); return {status:'expired'}; },
  onChange: state => states.push(state),
  onConfirmed: async () => assert.fail('expired code'),
});
const oldStart = retry.start(); retry.close(); await retry.start();
first.resolve({bind_id:'old',qrcode_img:'old-png'}); await oldStart;
assert.equal(states.at(-1)?.image,'new-png', 'old start cannot overwrite retry');
await tick(2000);
assert.equal(states.at(-1)?.phase,'expired');
assert.equal(timers.size,0);

const failure = createWeixinBindingFlow({
  start: async () => {throw new Error('本机授权已失效');},
  poll: async () => assert.fail('no QR received'),
  onChange: state => states.push(state),
  onConfirmed: async () => assert.fail('failed'),
});
await failure.start();
assert.equal(states.at(-1)?.phase,'error');
assert.equal(states.at(-1)?.tip,'本机授权已失效');
assert.equal(timers.size,0);
console.log('PASS: serialized QR polling, close/retry isolation, expiry and registration failure');

const {
  setHybridDual, setCapabilityPlaneReady, prepareChannelLocalBinding,
  startWeixinBind, getWeixinBindStatus, createChannelBot,
} = await import('../src/api');
setHybridDual(true); setCapabilityPlaneReady(true);
const requests: Array<{url: string; headers: Headers; body: unknown}> = [];
globalThis.fetch = async (url, init) => {
  requests.push({url: String(url), headers: new Headers(init?.headers), body: init?.body ? JSON.parse(String(init.body)) : null});
  const data = String(url).includes('/local-binding')
    ? {binding_id:'device-grant',device_name:'PC'}
    : String(url).includes('/bind/start') ? {bind_id:'binding',qrcode_img:'png'}
    : String(url).includes('/status') ? {status:'waiting'}
    : {channel_id:'channel',execution_location:'local'};
  return Response.json({code:10000,message:'Success',data});
};
const grant = await prepareChannelLocalBinding();
assert.equal(requests.at(-1)?.headers.get('x-hugagent-target'),'local');
await startWeixinBind('agent',grant.binding_id);
assert.equal(requests.at(-1)?.headers.get('x-hugagent-target'),null, 'QR channel login belongs to cloud');
assert(requests.at(-1)?.url.includes('execution_location=local'));
assert(requests.at(-1)?.url.includes('local_binding_id=device-grant'));
await getWeixinBindStatus('binding');
assert.equal(requests.at(-1)?.headers.get('x-hugagent-target'),null);
for (const channel of ['lark','dingtalk','wecom']) {
  await createChannelBot({channel_type:channel,app_id:'app',app_secret:'fixture',transport:'long_conn',
    execution_location:'local',local_binding_id:grant.binding_id});
  assert.equal(requests.at(-1)?.headers.get('x-hugagent-target'),null);
  assert.equal((requests.at(-1)?.body as {local_binding_id:string}).local_binding_id,'device-grant');
}
setHybridDual(false);
await assert.rejects(prepareChannelLocalBinding(),/混合模式/);
console.log('PASS: local registration routes to device; all channel bindings/status route to cloud with canonical envelopes');

// A five-minute deadline stops UI work even if a long poll never responds.
const stalled = deferred<{status: string}>();
const deadline = createWeixinBindingFlow({
  start: async () => ({bind_id:'deadline',qrcode_img:'png'}),
  poll: async () => stalled.promise,
  onChange: state => states.push(state),
  onConfirmed: async () => assert.fail('expired attempt'),
});
await deadline.start(); await tick(2000); await tick(300_000);
assert.equal(states.at(-1)?.phase,'expired');
stalled.resolve({status:'confirmed'}); await flush();
assert.equal(states.at(-1)?.phase,'expired');
assert.equal(timers.size,0);

let statusReads=0;
const success = createWeixinBindingFlow({
  start: async () => ({bind_id:'success',qrcode_img:'png'}),
  poll: async () => ++statusReads === 1 ? {status:'scanned'} : {status:'confirmed',channel_id:'bound'},
  onChange: state => states.push(state),
  onConfirmed: async () => {confirmed+=1;},
});
await success.start(); await tick(2000);
assert.equal(states.at(-1)?.phase,'scanned');
await tick(2000);
assert.equal(confirmed,1);
assert.equal(states.at(-1)?.open,false);
assert.equal(timers.size,0);
const empty = createWeixinBindingFlow({
  start:async () => ({bind_id:'empty',qrcode_img:''}),poll:async () => assert.fail('missing image'),
  onChange:state=>states.push(state),onConfirmed:async()=>assert.fail('missing image'),
});
await empty.start();
assert.equal(states.at(-1)?.phase,'error');
assert.equal(timers.size,0);
console.log('PASS: stalled-request deadline, scanned/confirmed success, empty QR failure');
