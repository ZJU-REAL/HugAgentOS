import type { ChatMessage } from '../types';

/** 打开会话先渲染多少条历史；往上滚续拉也按这个粒度。
 *
 *  过去是把每一页都拉完再一次性渲染整段历史——跑过大文件的长对话，光这一步就能
 *  把浏览器压垮。这里只铺第一屏，剩下的交给滚动续拉。 */
export const MESSAGE_PAGE_SIZE = 30;

/** 内存里保留完整历史的会话数（含当前会话）。 */
export const RETAINED_LOADED_CHATS = 6;

/** 最近加载或打开的顺序（新的在后）。只记 id，不持有消息。 */
export function touchRecentChat(recent: string[], chatId: string): string[] {
  if (recent[recent.length - 1] === chatId) return recent;
  return [...recent.filter((id) => id !== chatId), chatId];
}

/** 超出保留数、且允许淘汰的会话 id（最久未访问的在前）。 */
export function pickEvictableChats(
  recent: string[],
  canEvict: (chatId: string) => boolean,
  retain = RETAINED_LOADED_CHATS,
): string[] {
  const overflow = recent.length - retain;
  if (overflow <= 0) return [];
  return recent.slice(0, overflow).filter(canEvict);
}

/** 历史页只下发工具结果的梗概，展开卡片才按需取全文（见 ToolCallRow）。这些按需取回的
 *  全文才是会无限攒的大头：丢掉它们并恢复成"截断、可重取"，再展开时会重新请求。
 *  取不回来的（缺 tool id 或消息 id）原样保留，绝不让卡片变成打不开的空壳。 */
function dropFetchedToolOutputs(m: ChatMessage): ChatMessage {
  if (!m.messageId || !Array.isArray(m.toolCalls)) return m;
  if (!m.toolCalls.some((tc) => tc?.outputLoaded && tc.id)) return m;
  return {
    ...m,
    toolCalls: m.toolCalls.map((tc) => (
      tc?.outputLoaded && tc.id
        ? { ...tc, output: undefined, outputLoaded: undefined, outputTruncated: true }
        : tc
    )),
  };
}

/** 把被淘汰会话的内存消息裁小：只留最近一页（切回时首页整页覆盖它们），
 *  并丢掉按需取回的工具全文。返回 null 表示无需改动。 */
export function trimEvictedMessages(
  messages: ChatMessage[],
  keep = MESSAGE_PAGE_SIZE,
): ChatMessage[] | null {
  const tail = messages.length > keep ? messages.slice(-keep) : messages;
  let mutated = tail !== messages;
  const trimmed = tail.map((m) => {
    const next = dropFetchedToolOutputs(m);
    if (next !== m) mutated = true;
    return next;
  });
  return mutated ? trimmed : null;
}
