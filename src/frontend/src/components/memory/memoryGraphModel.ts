import type { MemoryGraphRelation, WikiGraphData } from '../../types';
import { roleOf, type MemoryEntityRole } from './memoryGraphTheme';

/**
 * 把 L3 的关系三元组摊成一张图：实体去重成点，关系原样成边。
 *
 * 单独成文件而不是写在组件的 useMemo 里，是为了能脱离浏览器直接验——见
 * scripts/test-memory-graph.ts。
 */

export interface MemoryEntityStat {
  name: string;
  role: MemoryEntityRole;
  /** 相异邻居数：决定节点大小，也决定图上画不画「还有邻居没显示」的虚线环。
   *  用相异邻居而不是关系条数——同两个实体之间有多条关系时，按条数会让节点凭空
   *  多出一圈「还有邻居」的暗示。 */
  neighbours: number;
}

export function buildEntityStats(
  relations: MemoryGraphRelation[],
): Map<string, MemoryEntityStat> {
  const outgoing = new Map<string, number>();
  const incoming = new Map<string, number>();
  const neighbours = new Map<string, Set<string>>();
  const link = (a: string, b: string) => {
    if (!neighbours.has(a)) neighbours.set(a, new Set());
    neighbours.get(a)!.add(b);
  };
  for (const r of relations) {
    outgoing.set(r.source, (outgoing.get(r.source) || 0) + 1);
    incoming.set(r.target, (incoming.get(r.target) || 0) + 1);
    link(r.source, r.target);
    link(r.target, r.source);
  }
  const stats = new Map<string, MemoryEntityStat>();
  for (const [name, linked] of neighbours) {
    stats.set(name, {
      name,
      role: roleOf(outgoing.get(name) || 0, incoming.get(name) || 0),
      neighbours: linked.size,
    });
  }
  return stats;
}

/** 按图例隐藏的角色过滤后成图；两端有一端被隐藏的关系整条不画 */
export function buildGraphData(
  stats: Map<string, MemoryEntityStat>,
  relations: MemoryGraphRelation[],
  hiddenRoles: Set<string>,
): WikiGraphData {
  const nodes = Array.from(stats.values())
    .filter((e) => !hiddenRoles.has(e.role))
    .map((e) => ({
      slug: e.name,
      title: e.name,
      page_type: e.role,
      link_count: e.neighbours,
    }));
  const keep = new Set(nodes.map((n) => n.slug));
  return {
    nodes,
    edges: relations
      .filter((r) => keep.has(r.source) && keep.has(r.target))
      .map((r) => ({ source: r.source, target: r.target, label: r.relationship })),
  };
}
