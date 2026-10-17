import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createDeveloperProxy } from './developer-proxy.mjs';
for (const url of ['file:///tmp', 'http://user:password@host', 'https://host/api', 'https://host/?target=other']) {
 assert.throws(() => createDeveloperProxy({ HUGAGENT_BACKEND_URL: url }));
}
assert.deepEqual(createDeveloperProxy({}), {});
const a = createDeveloperProxy({ HUGAGENT_BACKEND_URL: 'https://backend.example' });
const b = createDeveloperProxy({ HUGAGENT_BACKEND_URL: 'https://other.example' });
function cookie(proxyConfig) {
 const proxy = new EventEmitter(); proxyConfig['^/api(?:/|$)'].configure(proxy);
 const response = { headers: { 'set-cookie': ['jx_session=secret; Domain=backend.example; Secure; SameSite=Lax', 'unrelated=deny'] } };
 proxy.emit('proxyRes', response);
 const cookies = response.headers['set-cookie'];
 assert.equal(cookies.length, 1); assert.ok(!cookies[0].includes('Domain='));
 assert.ok(cookies[0].includes('; HttpOnly')); assert.ok(cookies[0].includes('Secure; SameSite=Lax'));
 return { proxy, value: cookies[0].split(';')[0] };
}
const first = cookie(a), second = cookie(b); assert.notEqual(first.value, second.value);
const headers = new Map([['authorization', 'secret'], ['cookie', 'unrelated=x']]);
const request = { removeHeader: name => headers.delete(name), setHeader: (name,value) => headers.set(name,value) };
first.proxy.emit('proxyReq', request, { headers: { host: 'localhost:4310', cookie: 'unrelated=x; ' + second.value } });
assert.equal(headers.has('cookie'), false); assert.equal(headers.has('authorization'), false);
first.proxy.emit('proxyReq', request, { headers: { host: 'localhost:4310', cookie: 'unrelated=x; ' + first.value } });
assert.equal(headers.get('cookie'), 'jx_session=secret');
console.log('PASS fixed target validation, Secure/HttpOnly retention, cross-target cookie isolation and authorization filtering');
