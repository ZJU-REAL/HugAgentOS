import assert from 'node:assert/strict';
import { createDesktopCapabilityStore } from '../src/stores/desktopCapabilityState';
import { getDeviceCapabilities } from '../src/api';
import type { DeviceCapabilityItem, DeviceCapabilityListing } from '../src/api';
const listing = (name: string): DeviceCapabilityListing => ({
  kind: 'skill', profile_id: name,
  items: [{ install_id: 'skill:' + name + ':' + name, runtime_name: name, source: 'cloud', kind: 'skill' } as DeviceCapabilityItem],
});
async function run() {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    assert.match(String(url), /desktop\/capabilities\/installations\?kind=skill/);
    assert.equal(new Headers(init?.headers).get('x-hugagent-target'), 'local');
    return new Response(JSON.stringify({ code: 0, data: listing('one') }));
  };
  try { assert.equal((await getDeviceCapabilities('skill')).items[0].runtime_name, 'one'); }
  finally { globalThis.fetch = originalFetch; }
  let calls = 0;
  let enabled = true;
  let resolve!: (value: DeviceCapabilityListing) => void;
  const store = createDesktopCapabilityStore({
    list: async () => { calls += 1; return new Promise((done) => { resolve = done; }); },
  }, () => enabled);
  const first = store.getState().load('skill');
  const duplicate = store.getState().load('skill');
  assert.equal(calls, 1);
  resolve(listing('first'));
  await Promise.all([first, duplicate]);
  await store.getState().load('skill');
  assert.equal(calls, 1, 'loaded source labels are cached');
  assert.equal(store.getState().kinds.skill.byName.first.source, 'cloud');
  const stale = store.getState().load('skill', true);
  const finishOld = resolve;
  store.getState().reset();
  const current = store.getState().load('skill');
  finishOld(listing('old-account'));
  await stale;
  assert.equal(store.getState().kinds.skill.loaded, false, 'old account cannot publish');
  resolve(listing('new-account'));
  await current;
  assert.deepEqual(Object.keys(store.getState().kinds.skill.byName), ['new-account']);
  store.getState().reset();
  enabled = false;
  await store.getState().load('skill');
  assert.equal(calls, 3, 'single mode does not load local capability labels');
  const retryStore = createDesktopCapabilityStore({ list: async () => {
    if (++calls === 4) throw new Error('offline');
    return listing('retry');
  } }, () => true);
  await retryStore.getState().load('skill');
  assert.equal(retryStore.getState().kinds.skill.loaded, false);
  await retryStore.getState().load('skill');
  assert.equal(retryStore.getState().kinds.skill.byName.retry.source, 'cloud');
  // These rows mirror a device upgraded from local defaults to cloud plugins.
  // The cloud card must retain its source even when its local predecessor is first.
  for (const kind of ['plugin', 'skill', 'mcp', 'agent'] as const) {
    for (const reverse of [false, true]) {
      const local = { install_id: kind + ':local:shared', runtime_name: 'shared', kind,
        source: 'local', change_state: 'new', resolution: { outcome: 'chosen' } } as DeviceCapabilityItem;
      const cloud = { install_id: kind + ':p_current:shared', runtime_name: 'shared', kind,
        source: 'cloud', change_state: 'synced', usable: false,
        resolution: { outcome: 'unusable' } } as DeviceCapabilityItem;
      const sameName = createDesktopCapabilityStore({ list: async () => ({
        kind, profile_id: 'p_current', items: reverse ? [cloud, local] : [local, cloud],
      }) }, () => true);
      await sameName.getState().load(kind);
      assert.equal(sameName.getState().kinds[kind].byName.shared.install_id, cloud.install_id,
        kind + ': cloud card must not inherit a local source or pending upload');
    }
  }
  const ambiguous = createDesktopCapabilityStore({ list: async () => ({
    kind: 'agent', profile_id: 'p_current', items: [
      { install_id: 'agent:p_current:first', runtime_name: 'same title', kind: 'agent', source: 'cloud' },
      { install_id: 'agent:p_current:second', runtime_name: 'same title', kind: 'agent', source: 'cloud' },
      { install_id: 'agent:p_previous:old', runtime_name: 'other account', kind: 'agent', source: 'cloud' },
    ],
  }) }, () => true);
  await ambiguous.getState().load('agent');
  assert.deepEqual(Object.keys(ambiguous.getState().kinds.agent.byName), [],
    'Ambiguous display names and stale cloud accounts must never supply an upload target');
  console.log('desktop capabilities: local routing, deduplication, cache, account isolation and retry passed');
}
void run().catch((error) => { console.error(error); process.exitCode = 1; });
