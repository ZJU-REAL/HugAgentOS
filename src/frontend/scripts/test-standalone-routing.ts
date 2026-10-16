import assert from 'node:assert/strict';
import { standaloneFromPath, pathForStandalone } from '../src/routing/standalone';
const agent = { kind: 'agents', resourceId: 'agent 你好' } as const;
const project = { kind: 'projects', resourceId: 'project-1' } as const;
assert.equal(pathForStandalone(agent), '/chat/agents/agent%20%E4%BD%A0%E5%A5%BD');
assert.deepEqual(standaloneFromPath(pathForStandalone(agent, 'chat #1')), { ...agent, chatId: 'chat #1' });
assert.deepEqual(standaloneFromPath('/chat/projects/project-1/'), project);
for (const path of ['/', '/c/1', '/chat/foo/1', '/chat/agents/%', '/chat/agents/a/c/', '/chat/agents/a/extra', '/chat/agents/a%2Fb']) assert.equal(standaloneFromPath(path), null);
console.log('Standalone route and history isolation checks passed');
