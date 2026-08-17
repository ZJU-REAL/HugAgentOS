/**
 * 侧边栏对话列表「手动拖拽顺序」的纯计算部分。
 *
 * 状态与持久化在 `stores/sidebarOrderStore.ts`，这里只放不碰浏览器 API 的算法，
 * 方便 `scripts/test-sidebar-order.ts` 直接跑。
 */

/** 排序只需要这三个字段，automation 虚拟项同样适用 */
export interface SidebarSortable {
  id: string;
  pinned?: boolean;
  updatedAt?: number;
}

/**
 * 把 `draggedId` 挪到 `targetId` 的前/后，返回该分组的新顺序。
 * 传入的 `groupIds` 必须是该组**当前可见顺序**——第一次拖拽就是靠它把整组
 * 冻结成显式顺序，之后 updatedAt 再变也顶不动别人。
 * 参数不合法（不同一组 / 自己拖自己）返回 null，调用方据此放弃这次拖拽。
 */
export function reorderGroupSequence(
  groupIds: string[],
  draggedId: string,
  targetId: string,
  place: 'before' | 'after',
): string[] | null {
  if (draggedId === targetId) return null;
  if (!groupIds.includes(draggedId) || !groupIds.includes(targetId)) return null;

  const withoutDragged = groupIds.filter((id) => id !== draggedId);
  const targetIdx = withoutDragged.indexOf(targetId);
  if (targetIdx < 0) return null;
  const insertAt = place === 'before' ? targetIdx : targetIdx + 1;
  return [
    ...withoutDragged.slice(0, insertAt),
    draggedId,
    ...withoutDragged.slice(insertAt),
  ];
}

/**
 * 把某个分组的新顺序并回全局顺序表：先摘掉该组的旧下标，再把新序列整体接到尾部。
 * 组间穿插不影响正确性——比较只发生在同组内部，只取相对下标。
 * 超长时从头部截断（头部是最久没动过的分组）。
 */
export function mergeGroupOrder(
  currentOrder: string[],
  groupIds: string[],
  nextGroupSeq: string[],
  maxLen: number,
): string[] {
  const groupSet = new Set(groupIds);
  const rest = currentOrder.filter((id) => !groupSet.has(id));
  return [...rest, ...nextGroupSeq].slice(-maxLen);
}

/**
 * 侧边栏条目排序：置顶优先 → 手动顺序 → updatedAt 倒序。
 * 手动顺序里没有的条目（新会话 / 从没拖过）排在有手动顺序的之前，让新会话照常冒头。
 */
export function compareSidebarItems(
  a: SidebarSortable,
  b: SidebarSortable,
  manualIndex: Map<string, number>,
): number {
  const pinDiff = Number(!!b.pinned) - Number(!!a.pinned);
  if (pinDiff !== 0) return pinDiff;
  const ia = manualIndex.get(a.id);
  const ib = manualIndex.get(b.id);
  if (ia === undefined && ib === undefined) return (b.updatedAt || 0) - (a.updatedAt || 0);
  if (ia === undefined) return -1;
  if (ib === undefined) return 1;
  return ia - ib;
}
