import assert from 'node:assert/strict';

import { buildEntityStats, buildGraphData } from '../src/components/memory/memoryGraphModel';
import type { MemoryGraphRelation } from '../src/types';

const RELATIONS: MemoryGraphRelation[] = [
  { source: '张三', relationship: '就职于', target: '信息中心' },
  { source: '张三', relationship: '负责', target: '政务云迁移' },
  { source: '政务云迁移', relationship: '依赖', target: '信息中心' },
  { source: '李四', relationship: '协助', target: '政务云迁移' },
  // 同一对实体之间的第二条关系：不能让节点看起来多出一个邻居
  { source: '张三', relationship: '汇报给', target: '信息中心' },
];

// ── 实体去重、角色判定、邻居计数 ──
{
  const stats = buildEntityStats(RELATIONS);
  assert.deepEqual(
    new Set(stats.keys()),
    new Set(['张三', '信息中心', '政务云迁移', '李四']),
    '实体应按名字去重',
  );

  // 只发出关系 → 主体；只承接 → 关联对象；两头都有 → 枢纽
  assert.equal(stats.get('张三')!.role, 'subject');
  assert.equal(stats.get('李四')!.role, 'subject');
  assert.equal(stats.get('信息中心')!.role, 'object');
  assert.equal(stats.get('政务云迁移')!.role, 'hub');

  // 张三 → 信息中心有两条关系，但邻居只有「信息中心 / 政务云迁移」两个
  assert.equal(stats.get('张三')!.neighbours, 2);
  assert.equal(stats.get('政务云迁移')!.neighbours, 3);
}

// ── 成图：每条关系都带上关系名 ──
{
  const stats = buildEntityStats(RELATIONS);
  const graph = buildGraphData(stats, RELATIONS, new Set());
  assert.equal(graph.nodes.length, 4);
  assert.equal(graph.edges.length, RELATIONS.length, '关系一条都不能丢');
  assert.ok(
    graph.edges.every((e) => !!e.label),
    '每条边都要带关系名，否则图上读不出这是什么关系',
  );
  assert.deepEqual(
    graph.edges.filter((e) => e.source === '张三' && e.target === '信息中心').map((e) => e.label),
    ['就职于', '汇报给'],
    '同一对实体之间的多条关系要各自成边',
  );
  // 节点大小取相异邻居数
  assert.equal(graph.nodes.find((n) => n.slug === '张三')!.link_count, 2);
}

// ── 图例隐藏某一类：该类节点连同它的边一起消失 ──
{
  const stats = buildEntityStats(RELATIONS);
  const graph = buildGraphData(stats, RELATIONS, new Set(['object']));
  assert.ok(
    graph.nodes.every((n) => n.slug !== '信息中心'),
    '被隐藏角色的节点不该出现',
  );
  assert.ok(
    graph.edges.every((e) => e.source !== '信息中心' && e.target !== '信息中心'),
    '悬空的边要一起去掉，否则力仿真会找不到端点',
  );
  assert.equal(graph.edges.length, 2);
}

// ── 空关系 ──
{
  const graph = buildGraphData(buildEntityStats([]), [], new Set());
  assert.equal(graph.nodes.length, 0);
  assert.equal(graph.edges.length, 0);
}

console.log('[test-memory-graph] 通过');
