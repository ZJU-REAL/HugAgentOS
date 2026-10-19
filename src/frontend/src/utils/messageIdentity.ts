import type { ChatMessage } from '../types';
import { FRESH_ENTER_WINDOW_MS } from './motionTokens';
/** 本地新建消息的身份。
 *
 *  后端主键 message_id 只有落库之后才拿得到，在那之前这条消息同样要有一个不会
 *  重复的身份——否则渲染只能退回时间戳，而同一轮的提问与回答是后端在同一时刻
 *  建的，时间戳必然撞车。 */
let counter = 0;
const entrances = new Map<string, number>();

export function newMessageUid(animate = false): string {
  counter += 1;
  const now = Date.now();
  for (const [uid, created] of entrances) if (now - created >= FRESH_ENTER_WINDOW_MS) entrances.delete(uid);
  const uid = `local_${now.toString(36)}_${counter.toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
  if (animate) {
    if (entrances.size >= 50) entrances.delete(entrances.keys().next().value!);
    entrances.set(uid, now);
  }
  return uid;
}

/** Read during render; consume after commit so StrictMode render retries stay pure. */
export function hasMessageEntrance(uid: string): boolean {
  const created = entrances.get(uid);
  return created !== undefined && Date.now() - created < FRESH_ENTER_WINDOW_MS;
}
export function completeMessageEntrance(uid: string) { entrances.delete(uid); }

/** Bind admission to the exact optimistic row, including a raced history response.
 * Content and timestamps are not identities: identical text can be sent twice.
 */
export function acknowledgeUserMessage(messages: ChatMessage[], uid: string, messageId: string): ChatMessage[] {
  if (!messageId || !messages.some(m => m.uid === uid && m.role === 'user')) return messages;
  return messages.flatMap(m => {
    if (m.uid === uid && m.role === 'user') return [{ ...m, messageId }];
    if (m.role === 'user' && m.messageId === messageId) return [];
    return [m];
  });
}
