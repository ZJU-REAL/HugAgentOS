import assert from 'node:assert/strict';

import {
  compareSidebarItems,
  mergeGroupOrder,
  reorderGroupSequence,
  type SidebarSortable,
} from '../src/utils/sidebarOrder';

const indexOf = (order: string[]) => new Map(order.map((id, i) => [id, i] as const));

{
  // 往上拖：落到目标之前
  assert.deepEqual(reorderGroupSequence(['a', 'b', 'c'], 'c', 'a', 'before'), ['c', 'a', 'b']);
  // 往下拖：落到目标之后
  assert.deepEqual(reorderGroupSequence(['a', 'b', 'c'], 'a', 'c', 'after'), ['b', 'c', 'a']);
  // 落到中间一条的上/下沿
  assert.deepEqual(reorderGroupSequence(['a', 'b', 'c'], 'a', 'c', 'before'), ['b', 'a', 'c']);
  assert.deepEqual(reorderGroupSequence(['a', 'b', 'c'], 'c', 'a', 'after'), ['a', 'c', 'b']);
}

{
  // 自己拖到自己身上、跨组拖（id 不在本组）都不产生新顺序
  assert.equal(reorderGroupSequence(['a', 'b'], 'a', 'a', 'before'), null);
  assert.equal(reorderGroupSequence(['a', 'b'], 'x', 'a', 'before'), null);
  assert.equal(reorderGroupSequence(['a', 'b'], 'a', 'x', 'after'), null);
}

{
  // 并回全局顺序表：本组旧下标被摘掉，新序列整体接到尾部，别的组原样保留
  assert.deepEqual(
    mergeGroupOrder(['p1', 'a', 'p2', 'b'], ['a', 'b'], ['b', 'a'], 500),
    ['p1', 'p2', 'b', 'a'],
  );
  // 超长时从头部截断（头部＝最久没动过的分组）
  assert.deepEqual(mergeGroupOrder(['x', 'y'], ['a'], ['a'], 2), ['y', 'a']);
}

{
  const item = (id: string, updatedAt: number, pinned = false): SidebarSortable =>
    ({ id, updatedAt, pinned });

  // 没有手动顺序时 = 旧行为：置顶优先，其余按 updatedAt 倒序
  const empty = indexOf([]);
  const byDefault = [item('a', 1), item('b', 3), item('c', 2, true)]
    .sort((x, y) => compareSidebarItems(x, y, empty))
    .map((i) => i.id);
  assert.deepEqual(byDefault, ['c', 'b', 'a']);

  // 手动顺序压过 updatedAt：a 被拖到最前，之后 b 更新也顶不动它
  const manual = indexOf(['a', 'b', 'c']);
  const dragged = [item('b', 999), item('c', 2), item('a', 1)]
    .sort((x, y) => compareSidebarItems(x, y, manual))
    .map((i) => i.id);
  assert.deepEqual(dragged, ['a', 'b', 'c']);

  // 置顶仍然是外层键：手动顺序只在同一置顶带内生效
  const withPinned = [item('b', 5), item('c', 4, true), item('a', 3)]
    .sort((x, y) => compareSidebarItems(x, y, manual))
    .map((i) => i.id);
  assert.deepEqual(withPinned, ['c', 'a', 'b']);

  // 手动顺序里没有的新会话排在最前，按 updatedAt 倒序（新会话照常冒头）
  const withFresh = [item('a', 1), item('new1', 10), item('new2', 20), item('b', 2)]
    .sort((x, y) => compareSidebarItems(x, y, manual))
    .map((i) => i.id);
  assert.deepEqual(withFresh, ['new2', 'new1', 'a', 'b']);
}

console.log('sidebar order tests passed');
