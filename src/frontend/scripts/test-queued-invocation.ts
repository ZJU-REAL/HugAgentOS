import assert from 'node:assert/strict';

import {
  chatInvocationMessageProps,
  chatInvocationRequestFields,
  createQueuedChatTurn,
  hasChatInvocation,
  normalizeChatInvocation,
  queuedChatInvocation,
} from '../src/utils/chatInvocation';

const queued = createQueuedChatTurn({
  id: 'steer-1',
  content: '请继续处理',
  createdAt: 1,
  source: {
    activeSkill: { id: 'skill-direct', name: '公文写作' },
    activePlugin: {
      id: 'office@user-1',
      name: '办公插件',
    },
    activeConnector: { id: 'mcp-drive', name: '云盘' },
    activeMention: { id: 'agent-reviewer', name: '审校智能体' },
  },
});

const restoredQueued = JSON.parse(JSON.stringify(queued)) as unknown;
const invocation = queuedChatInvocation(restoredQueued);

assert.equal(hasChatInvocation(invocation), true);
assert.deepEqual(chatInvocationRequestFields(invocation), {
  skill_id: 'skill-direct',
  skill_name: '公文写作',
  plugin_id: 'office@user-1',
  plugin_name: '办公插件',
  connector_id: 'mcp-drive',
  connector_name: '云盘',
  mention_agent_id: 'agent-reviewer',
  mention_name: '审校智能体',
});
assert.deepEqual(chatInvocationMessageProps(invocation), {
  skillId: 'skill-direct',
  skillName: '公文写作',
  pluginName: '办公插件',
  connectorName: '云盘',
  mentionName: '审校智能体',
});

assert.deepEqual(normalizeChatInvocation({
  skill: { id: 'skill-1', name: '技能 1' },
  plugin: { id: 'plugin-1@global', name: '插件 1', skillIds: ['a', '', 'a'], mcpIds: ['b', 'b'] },
  connector: { id: 'connector-1', name: '连接器 1' },
  mention: { id: 'agent-1', name: '智能体 1' },
}), {
  skill: { id: 'skill-1', name: '技能 1' },
  plugin: { id: 'plugin-1@global', name: '插件 1' },
  connector: { id: 'connector-1', name: '连接器 1' },
  mention: { id: 'agent-1', name: '智能体 1' },
});

assert.deepEqual(normalizeChatInvocation({ plugin: { name: '', skillIds: [], mcpIds: [] } }), {});
assert.deepEqual(normalizeChatInvocation({
  plugin: { name: '旧版插件', skillIds: ['legacy-skill'], mcpIds: [] },
}), {});
assert.equal(hasChatInvocation({}), false);
assert.deepEqual(queuedChatInvocation({ invocation: {} }), {});

console.log('queued invocation tests passed');
