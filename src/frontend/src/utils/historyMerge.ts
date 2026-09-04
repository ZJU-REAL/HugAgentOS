import type { ChatMessage } from '../types';

/**
 * 把后端返回的**一页**历史并进本地消息列表——这是历史写入本地的唯一路径。
 *
 * 一页只是窗口：按 message_id 找到它与本地列表的交叠区间，区间内以服务端为准，
 * 区间两侧本地独有的消息原样保留。位置靠 message_id 索引而不比时间戳——本地 ts
 * 来自浏览器时钟，服务端 created_at 是另一个时钟。
 * 例外只有一条：本标签页正在实时接收的那条气泡以本地为准。
 */
export interface MergeHistoryOptions {
  /** 本标签页正在给这个会话喂流。 */
  localIsWriter?: boolean;
}

export function mergeHistoryPage(
  local: ChatMessage[],
  page: ChatMessage[],
  opts: MergeHistoryOptions = {},
): ChatMessage[] {
  if (page.length === 0) return local;
  if (local.length === 0) return page;

  const localById = new Map<string, ChatMessage>();
  for (const m of local) if (m.messageId) localById.set(m.messageId, m);
  const resolved = page.map((m) => {
    const mine = m.messageId ? localById.get(m.messageId) : undefined;
    return mine?.isStreaming && opts.localIsWriter ? mine : m;
  });

  const pageIds = new Set(page.map((m) => m.messageId).filter(Boolean) as string[]);
  const overlaps = (m: ChatMessage) => !!m.messageId && pageIds.has(m.messageId);

  const first = local.findIndex(overlaps);
  if (first < 0) {
    // 本地这份与这一页毫无交集：本地要么是刚建的会话，要么只剩没落库的本轮消息。
    // 落库的一页在前，未落库的接在后面，一条都不丢。
    return [...resolved, ...local.filter((m) => !m.messageId)];
  }
  let last = local.length - 1;
  while (last >= 0 && !overlaps(local[last])) last -= 1;
  return [...local.slice(0, first), ...resolved, ...local.slice(last + 1)];
}
