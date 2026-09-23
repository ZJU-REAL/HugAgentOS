import assert from 'node:assert/strict';
import { createCapabilitySyncStore } from '../src/stores/capabilitySyncState';

let calls = 0;
let reply!: (value: { changed: boolean | null }) => void;
const store = createCapabilitySyncStore({
  check: async () => { calls++; return new Promise(resolve => { reply = resolve; }); },
  sync: async () => {},
  afterSync: () => {},
}, () => true);
const request = store.getState().check();
void store.getState().check();
assert.equal(calls, 1, 'concurrent observations share a check');
assert.equal(store.getState().pending, false, 'global signal alone never lights the button');
reply({ changed: false });
await request;
assert.equal(store.getState().pending, false, 'no account change means no prompt');


await store.getState().check();
assert.equal(calls, 1, 'ordinary activity is rate limited');
store.getState().reset();
const changed = store.getState().check();
reply({ changed: true });
await changed;
assert.equal(store.getState().pending, true, 'real difference lights the entry');
const unknown = store.getState().check(true);
reply({ changed: null });
await unknown;
assert.equal(store.getState().pending, true, 'unknown cannot clear a known change');

// A check started before a reset must never mark the replacement account.
const old = store.getState().check(true);
const finishOld = reply;
store.getState().reset();
const next = store.getState().check();
finishOld({ changed: true });
await old;
assert.equal(store.getState().pending, false);
reply({ changed: false });
await next;

// A response from before synchronization must not resurrect the button.
store.getState().reset();
const beforeSync = store.getState().check();
const finishBeforeSync = reply;
const sync = store.getState().run();
await Promise.resolve(); // synchronization finishes and starts its fresh check
reply({ changed: false });
await sync;
finishBeforeSync({ changed: true });
await beforeSync;
assert.equal(store.getState().pending, false);

// Sync errors preserve the entry. Account changes discard late sync effects.
let synced!: () => void;
let afterSync = 0;
let enabled = true;
let clock = 0;
const updates = createCapabilitySyncStore({
  check: async () => ({ changed: true }),
  sync: async () => new Promise<void>(resolve => { synced = resolve; }),
  afterSync: () => { afterSync++; },
}, () => enabled, () => clock);
await updates.getState().check();
const oldSync = updates.getState().run();
updates.getState().reset();
synced();
await oldSync;
assert.equal(afterSync, 0);
assert.equal(updates.getState().pending, false);
enabled = false;
await updates.getState().check(true);
await updates.getState().run();
assert.equal(updates.getState().pending, false, 'non-dual/unready mode does not check or sync');

const failures = createCapabilitySyncStore({
  check: async () => ({ changed: true }),
  sync: async () => { throw new Error('sync failed'); },
  afterSync: () => { throw new Error('must not refresh'); },
}, () => true);
await failures.getState().check();
await failures.getState().run();
assert.equal(failures.getState().pending, true);
assert.equal(failures.getState().busy, false);
assert.equal(failures.getState().error, 'sync failed');

let checks = 0;
const activity = createCapabilitySyncStore({
  check: async () => { checks++; return { changed: false }; },
  sync: async () => {},
  afterSync: () => {},
}, () => true, () => clock);
await activity.getState().check();
clock = 59_999;
await activity.getState().check();
assert.equal(checks, 1);
clock = 60_000;
await activity.getState().check();
assert.equal(checks, 2, 'content edits are discoverable even if global epoch stays unchanged');

// Exercise the actual API observer: global epoch oscillation is not an update.
const api = await import('../src/api');
api.setHybridDual(true);
api.setCapabilityPlaneReady(true);
let hints = 0;
api.setCapabilityCheckListener(() => { hints++; });
globalThis.fetch = async (_url, init) => {
  const local = new Headers(init?.headers).get('x-hugagent-target') === 'local';
  return new Response(JSON.stringify({ code: 0, data: local ? { changed: false } : {} }), {
    headers: { 'x-hugagent-capability-epoch': hints % 2 ? 'old-worker' : 'new-worker' },
  });
};
await api.apiRequest('/v1/me', undefined, 'cloud');
await api.apiRequest('/v1/me', undefined, 'cloud');
assert.equal(hints, 2, 'successful cloud activity requests comparison, not pending=true');
assert.deepEqual(await api.checkDeviceCapabilitySync(), { changed: false });
assert.equal(hints, 2, 'local comparison cannot recursively request itself');
globalThis.fetch = async () => new Response('{}', { status: 500 });
await assert.rejects(() => api.apiRequest('/v1/me', undefined, 'cloud'));
assert.equal(hints, 2, 'failed requests cannot signal updates');
api.setHybridDual(false);
globalThis.fetch = async () => new Response('{}');
await api.apiRequest('/v1/me');
assert.equal(hints, 2, 'web/cloud-only mode never schedules checks');
console.log('capability sync: comparison, throttling, account/sync races, errors and API routing passed');
