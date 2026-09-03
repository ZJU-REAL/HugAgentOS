import assert from 'node:assert/strict';

import {
  AUTOMATION_CHAT_TEMPLATE,
  AUTOMATION_PLUGIN_SLUG,
  resolveAutomationPluginReference,
} from '../src/utils/automationConversation';

assert.equal(AUTOMATION_PLUGIN_SLUG, 'automation');
assert.equal(
  AUTOMATION_CHAT_TEMPLATE,
  '我要创建一个定时任务，每【时间间隔】执行【具体任务】',
);

const plugin = resolveAutomationPluginReference([
  {
    install_id: 'automation@global',
    slug: 'automation',
    name: '定时任务管理',
    version: '1.0.0',
    description: '',
    category: '效率工具',
    source: 'builtin',
    enabled: true,
    skills: ['scheduled-tasks'],
    mcp: ['automation_task'],
    import_report: { skills: [], mcp: [], dropped: [] },
  },
]);

assert.deepEqual(plugin, {
  id: 'automation@global',
  name: '定时任务管理',
});

assert.equal(
  resolveAutomationPluginReference([
    {
      install_id: 'automation@global',
      slug: 'automation',
      name: '定时任务管理',
      version: '1.0.0',
      description: '',
      category: '效率工具',
      source: 'builtin',
      enabled: false,
      skills: ['scheduled-tasks'],
      mcp: ['automation_task'],
      import_report: { skills: [], mcp: [], dropped: [] },
    },
  ]),
  null,
);

console.log('automation conversation tests passed');
