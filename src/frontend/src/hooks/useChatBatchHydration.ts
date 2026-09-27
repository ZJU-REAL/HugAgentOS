import { useEffect } from 'react';
import { listActiveBatchPlans, getBatchPlan } from '../api';
import { useAuthStore, useBatchStore } from '../stores';
import type { BatchPlanMeta, BatchSourceType, BatchItemResult } from '../types';

export function useChatBatchHydration(currentChatId: string) {
  const { authUser, authChecking } = useAuthStore();
  // ── Reconnect / hydrate batch executions when this chat opens ──
  // - Active plans (confirmed/running): re-attach the SSE stream; the
  //   orchestrator runs as a detached server-side task so refresh
  //   doesn't kill it.
  // - Finished plans (done/failed): hydrate the panel directly from
  //   plan.item_results — no fake "in-progress" pulse, no replay flicker.
  useEffect(() => {
    if (authChecking || !authUser) return;
    const chatId = currentChatId;
    if (!chatId) return;
    let cancelled = false;
    (async () => {
      try {
        const plans = await listActiveBatchPlans(chatId);
        if (cancelled) return;
        // Hydrate finished plans + reconnect to running ones in parallel
        // so users with multiple historical batches don't wait for N
        // sequential GETs.
        await Promise.all(plans.map(async (p) => {
          const meta: BatchPlanMeta = {
            plan_id: p.plan_id,
            total: p.items_total,
            source_type: p.source_type as BatchSourceType,
            preview: p.items_preview as Record<string, unknown>[],
            default_template: p.prompt_template,
            placeholder_keys: p.placeholder_keys,
            chat_id: chatId,
          };

          if (p.status !== 'done' && p.status !== 'failed') {
            useBatchStore.getState().connectStream(p.plan_id, meta);
            return;
          }
          // Finished — pull the per-item results directly and render
          // as a static snapshot. Saves an SSE round-trip and keeps
          // the sidebar pulse off (work is already done).
          try {
            const detail = await getBatchPlan(p.plan_id);
            if (cancelled) return;
            const results = (detail.item_results || []).map((r) => ({
              index: r.index,
              status: r.status,
              content: r.content,
              error: r.error,
              retry_count: r.retry_count,
              // Backend returns these as opaque arrays; BatchItemBubble
              // normalizes snake_case → camelCase per item.
              tool_calls: (Array.isArray(r.tool_calls) ? r.tool_calls : undefined) as BatchItemResult['tool_calls'],
              artifacts: Array.isArray(r.artifacts) ? r.artifacts : undefined,
              citations: (Array.isArray(r.citations) ? r.citations : undefined) as BatchItemResult['citations'],
            } satisfies BatchItemResult));
            useBatchStore.getState().hydratePlan(
              meta,
              p.status as 'done' | 'failed',
              results,
              { success: detail.progress?.success ?? 0, failed: detail.progress?.failed ?? 0 },
            );
          } catch {
            // best-effort — chat still works without this plan
          }
        }));
      } catch {
        // best-effort — chat still works without reattach
      }
    })();
    return () => { cancelled = true; };
  }, [currentChatId, authUser, authChecking]);

}
