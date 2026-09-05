import type { ChatMessage } from '../types';

/** 一页历史里的计划痕迹：有没有计划消息；最后一个计划快照若仍是 preview，它的 plan_id。
 *  后者用来在刷新后让"确认执行"接着执行已有计划，而不是重新生成一个。 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function scanPlanSnapshots(items: any[]): { hasPlanMessages: boolean; pendingPlanId: string | null } {
  let hasPlanMessages = false;
  let pendingPlanId: string | null = null;
  for (const m of items) {
    const snap = m?.metadata?.plan_snapshot;
    if (m?.role !== 'assistant' || !snap) continue;
    hasPlanMessages = true;
    const pid = m?.metadata?.plan_id;
    pendingPlanId = ((snap as { mode?: string }).mode || 'complete') === 'preview' && pid ? String(pid) : null;
  }
  return { hasPlanMessages, pendingPlanId };
}

/** 历史消息后处理：把「已经决策过」的计划预览卡片标回已决策。
 *
 *  决策位（decided）只在本地内存里打，不进 plan_snapshot；所以拉一次历史之后，
 *  一张早就确认执行过、甚至已经被用户中断的计划预览卡，footer 又会重新长出
 *  「确认执行 / 放弃」两个按钮，看起来像这一轮从没发生过。这里按 planId 对账：
 *  同一个计划后面出现过执行态卡片 → 判定为已确认。 */
export function markResolvedPlanPreviews(messages: ChatMessage[]): ChatMessage[] {
  const executedPlanIds = new Set<string>();
  for (const msg of messages) {
    for (const seg of msg.segments || []) {
      const data = seg.type === 'plan' ? seg.planData : undefined;
      if (data?.planId && data.mode !== 'preview') executedPlanIds.add(data.planId);
    }
  }
  if (executedPlanIds.size === 0) return messages;
  return messages.map((msg) => {
    if (!msg.segments?.some((s) => s.type === 'plan'
      && s.planData?.mode === 'preview'
      && !s.planData.decided
      && !!s.planData.planId
      && executedPlanIds.has(s.planData.planId))) return msg;
    return {
      ...msg,
      segments: msg.segments.map((seg) => (
        seg.type === 'plan' && seg.planData?.mode === 'preview'
          && !seg.planData.decided && !!seg.planData.planId
          && executedPlanIds.has(seg.planData.planId)
          ? { ...seg, planData: { ...seg.planData, decided: 'confirmed' as const } }
          : seg
      )),
    };
  });
}
