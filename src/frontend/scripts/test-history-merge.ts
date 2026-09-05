import assert from 'node:assert/strict';

import type { ChatMessage } from '../src/types';
import { mergeHistoryPage } from '../src/utils/historyMerge';

const msg = (id: string | undefined, ts: number, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  role: 'assistant',
  content: id || `local-${ts}`,
  ts,
  ...(id ? { messageId: id } : {}),
  ...extra,
});

// 窗口之外更早的历史必须留下：后端只回最近一页，整列覆盖会把滚上去加载过的部分抹平。
{
  const local = [msg('m1', 1), msg('m2', 2), msg('m3', 3), msg('m4', 4)];
  const page = [msg('m3', 3), msg('m4', 4)];
  assert.deepEqual(
    mergeHistoryPage(local, page).map((m) => m.messageId),
    ['m1', 'm2', 'm3', 'm4'],
  );
}

// 还没落库的本轮消息必须留下：重载与流式输出撞车时不能把正在写的回答盖掉。
{
  const local = [msg('m1', 1), msg(undefined, 2), msg(undefined, 3)];
  const page = [msg('m1', 1)];
  const merged = mergeHistoryPage(local, page);
  assert.equal(merged.length, 3);
  assert.deepEqual(merged.map((m) => m.messageId), ['m1', undefined, undefined]);
}

// 交叠区间以后端为准——这正是重载的目的：拿回库里的最终版本。
{
  const local = [msg('m1', 1, { content: 'stale', isStreaming: false })];
  const page = [msg('m1', 1, { content: 'final' })];
  assert.equal(mergeHistoryPage(local, page)[0].content, 'final');
}

// 本标签页正在喂流的那一条以本地为准，不让半截的库内容顶掉活气泡。
{
  const local = [msg('m1', 1, { content: 'live', isStreaming: true })];
  const page = [msg('m1', 1, { content: 'partial' })];
  const merged = mergeHistoryPage(local, page, { localIsWriter: true });
  assert.equal(merged[0].content, 'live');
  assert.equal(merged[0].isStreaming, true);
}

// 本地与这一页毫无交集（刚建的会话 / 只剩未落库的消息）：两边都保留，落库的在前。
{
  const local = [msg(undefined, 9)];
  const page = [msg('m1', 1), msg('m2', 2)];
  assert.deepEqual(
    mergeHistoryPage(local, page).map((m) => m.messageId),
    ['m1', 'm2', undefined],
  );
}

// 空输入的边界：任一侧为空时原样返回另一侧。
{
  assert.deepEqual(mergeHistoryPage([msg('m1', 1)], []), [msg('m1', 1)]);
  assert.deepEqual(mergeHistoryPage([], [msg('m1', 1)]), [msg('m1', 1)]);
}

// 现场记录的那两次：整轮回复连同全部工具调用块一起消失，用户的提问还在、带原时间戳。
// 成因是这一轮跑到一半、助手消息还没落库（正常完成才会写库，所以丢失的轮次没有"用时"那行），
// 而这时任何一次历史重载都会拿只含已落库消息的快照把本地整列换掉。
{
  const local: ChatMessage[] = [
    msg('u1', 1, { role: 'user' }),
    msg('a1', 2),
    msg('u2', 3, { role: 'user' }),          // 用户的提问，已落库
    msg(undefined, 4, {                       // 跑到一半的这一轮，尚未落库
      isStreaming: true,
      toolCalls: [{ name: 'bash', status: 'running' }],
    }),
  ];
  const page = [msg('u1', 1, { role: 'user' }), msg('a1', 2), msg('u2', 3, { role: 'user' })];
  const merged = mergeHistoryPage(local, page);
  assert.equal(merged.length, 4);
  assert.equal(merged[3].isStreaming, true);
  assert.equal(merged[3].toolCalls?.length, 1);
}

// 页面被外力整体重建（用户遇到过页面自己变成 about:blank）后从 localStorage 恢复：
// 那一轮已经被判定不再流式，但仍然没有 message_id，同样不能被库里的快照抹掉。
{
  const local: ChatMessage[] = [
    msg('u2', 3, { role: 'user' }),
    msg(undefined, 4, { isStreaming: false, toolCalls: [{ name: 'bash', status: 'success' }] }),
  ];
  const page = [msg('u2', 3, { role: 'user' })];
  const merged = mergeHistoryPage(local, page);
  assert.equal(merged.length, 2);
  assert.equal(merged[1].toolCalls?.length, 1);
}

// 服务端那一行从轮次接纳起就存在并持续刷新（带 in_flight + event_offset）。本标签页没有
// 在喂这一轮的流时，它就是最新、可续接的基态，应当以它为准；正在实时接收时才保留本地。
{
  const local: ChatMessage[] = [
    msg('u1', 1, { role: 'user' }),
    msg('a1', 2, { isStreaming: true, content: 'local partial' }),
  ];
  const page: ChatMessage[] = [
    msg('u1', 1, { role: 'user' }),
    msg('a1', 2, { isStreaming: true, content: 'server partial', inFlight: { eventOffset: 42 } }),
  ];
  const detached = mergeHistoryPage(local, page, { localIsWriter: false });
  assert.equal(detached[1].content, 'server partial');
  assert.equal(detached[1].inFlight?.eventOffset, 42);
  const live = mergeHistoryPage(local, page, { localIsWriter: true });
  assert.equal(live[1].content, 'local partial');
}

// 另一个标签页把这一轮跑完了：本地还挂着"生成中"的旧气泡，服务端已定稿（没有 in_flight）。
// 本标签页没在接收流，就必须让定稿版本顶掉它，否则气泡会永远转圈。
{
  const local: ChatMessage[] = [msg('a1', 2, { isStreaming: true, content: 'stale' })];
  const page: ChatMessage[] = [msg('a1', 2, { content: 'final answer' })];
  const merged = mergeHistoryPage(local, page, { localIsWriter: false });
  assert.equal(merged[0].content, 'final answer');
  assert.equal(merged[0].isStreaming, undefined);
}

console.log('history merge tests passed');
