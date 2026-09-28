import assert from 'node:assert/strict';
import { agentApiExamples, resolveAgentApiEndpoint } from '../src/utils/agentApiExamples';

const web = { origin: 'https://agent.example', apiBase: '/api', isDesktop: false, local: false, serverBase: '', localBase: '' };
assert.equal(resolveAgentApiEndpoint(web), 'https://agent.example/api/v1/agents/responses');
assert.equal(resolveAgentApiEndpoint({ ...web, apiBase: 'https://api.example' }), 'https://api.example/v1/agents/responses');
assert.equal(resolveAgentApiEndpoint({ ...web, isDesktop: true, serverBase: 'https://cloud.example' }), 'https://cloud.example/api/v1/agents/responses');
assert.equal(resolveAgentApiEndpoint({ ...web, isDesktop: true, local: true, serverBase: 'https://cloud.example', localBase: 'http://127.0.0.1:32101' }), 'http://127.0.0.1:32101/v1/agents/responses');
assert.equal(resolveAgentApiEndpoint({ ...web, isDesktop: true }), '');
assert.equal(resolveAgentApiEndpoint({ ...web, isDesktop: true, local: true, serverBase: 'https://cloud.example' }), '', 'Missing local origin must not advertise a different backend');
for (const stream of [true, false]) {
  const example = agentApiExamples(resolveAgentApiEndpoint(web), stream, 'ua_one');
  assert.equal(example.payload.stream, stream);
  assert.equal(example.payload.agent_id, 'ua_one');
  assert.ok(example.curl.includes(`"stream": ${stream}`));
  assert.ok(example.curl.includes('"agent_id": "ua_one"'));
  assert.ok(example.python.includes(`"stream": ${stream ? 'True' : 'False'}`));
  assert.ok(example.python.includes('os.environ["AGENT_API_KEY"]'));
  assert.ok(example.python.includes(stream ? 'iter_lines' : 'response.json()'));
  assert.equal(example.curl.includes('curl -N'), stream);
  if (!stream) {
    assert.ok(example.python.includes('result = response.json()'));
    assert.ok(example.python.includes('print(result["response"])'), 'Non-streaming replies are top-level ChatResponse fields');
    assert.equal(example.python.includes('["data"]'), false, 'Agent responses have no management API envelope');
  }
}
const memory = new Map<string, string>();
const storage = { getItem: (key: string) => memory.get(key) ?? null, setItem: (key: string, value: string) => memory.set(key, value) };
Object.assign(globalThis, { document: { documentElement: {}, addEventListener() {} }, localStorage: storage, window: { addEventListener() {}, removeEventListener() {}, localStorage: storage, location: { origin: web.origin } } });
const api = await import('../src/api/agentApi');
const routing = await import('../src/api');
routing.setHybridDual(true);
routing.setCapabilityPlaneReady(true);
const requests: Array<{ path: string; options?: RequestInit }> = [];
globalThis.fetch = async (input, options) => {
  requests.push({ path: String(input), options });
  return Response.json({ code: 10000, data: { items: [], api_key: 'fake-created-key' } });
};
await api.listAgentApiKeys('agent/alpha');
await api.createAgentApiKey('agent/alpha', 'Integration', 30);
await api.toggleAgentApiKey('agent/alpha', 'key/1', false);
await api.revealAgentApiKey('agent/alpha', 'key/1');
await api.revokeAgentApiKey('agent/alpha', 'key/1');
const controller = new AbortController();
await api.listAgentApiCalls('agent/alpha', { page: 2, page_size: 10, key_id: 'key/1', status: 'failed' }, controller.signal);
assert.equal(requests.length, 6);
assert.ok(requests.every(request => request.path.startsWith('/api/v1/agents/agent%2Falpha/')));
assert.ok(requests.every(request => new Headers(request.options?.headers).get('x-hugagent-target') === 'local'), 'Keys and records follow the same backend as the agent');
assert.equal(requests[1].options?.method, 'POST');
assert.deepEqual(JSON.parse(String(requests[1].options?.body)), { name: 'Integration', expires_in_days: 30 });
assert.equal(requests[2].path, '/api/v1/agents/agent%2Falpha/api-keys/key%2F1');
assert.equal(requests[4].options?.method, 'DELETE');
assert.equal(requests[5].path, '/api/v1/agents/agent%2Falpha/api-calls?page=2&page_size=10&key_id=key%2F1&status=failed');
assert.equal(requests[5].options?.signal, controller.signal);
console.log('PASS: response examples, real deployment origins, scoped key CRUD, encoded paths, filters, cancellation, local routing');
