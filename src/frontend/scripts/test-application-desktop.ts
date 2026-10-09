import assert from 'node:assert/strict';
import { useDeploymentModeStore, stablePublicOrigin } from '../src/stores/deploymentModeStore';
(globalThis as unknown as { window: { location: { origin: string } } }).window =
  { location: { origin: 'http://127.0.0.1:59999' } };
for (const [isDesktop, serverBase, expected] of [
  [false, '', 'http://127.0.0.1:59999'],
  [true, 'https://cloud.example.test', 'https://cloud.example.test'],
  [true, 'http://127.0.0.1:32101', 'http://127.0.0.1:32101'],
] as const) {
  useDeploymentModeStore.setState({isDesktop, serverBase});
  assert.equal(stablePublicOrigin() + '/applications-mcp/app', expected + '/applications-mcp/app');
}
console.log('Web, cloud desktop and local desktop MCP origins passed');
