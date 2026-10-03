import assert from 'node:assert/strict';
import '../src/api';

const listeners: { onmessage: ((message: { data: string }) => void) | null }[] = [];
class FakeEvents {
  onmessage = null;
  constructor() { listeners.push(this); }
  close() {}
}
Object.assign(globalThis, {
  window: { __HG_DESKTOP__: { provision_mode: 'dual', server_base: 'https://cloud.example' } },
  EventSource: FakeEvents,
});
const { useDeploymentModeStore } = await import('../src/stores/deploymentModeStore');
const service = { phase: 'error', progress: 0, message: 'service failed', ready: false };
const bridge = { identity_ready: false, capabilities_ready: false, models_ready: false };
const frame = (value: unknown) => listeners.at(-1)!.onmessage!({ data: JSON.stringify(value) });
frame({ ...service, bridge });
assert.deepEqual(useDeploymentModeStore.getState().localService, service,
  'Tauri flattened setup status must reach the retry UI');
frame({ service: { ...service, phase: 'ready', progress: 100, ready: true }, bridge });
assert.equal(useDeploymentModeStore.getState().localService?.ready, true, 'UOS nested frames still work');
const previous = useDeploymentModeStore.getState();
frame({ service: previous.localService, bridge });
assert.equal(useDeploymentModeStore.getState(), previous, 'identical state frames do not notify subscribers');
console.log('desktop status: Tauri/UOS service state and unchanged-frame deduplication passed');
