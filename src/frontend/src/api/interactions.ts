import type { FileConfirmDecision, FileConfirmInfo, DesignPickInfo, UserQuestionRequest, UserQuestionAnswer } from '../types';
import { apiRequest, isHybridDual, unwrapData } from './core';

/** §13 MySpace write confirmation: out-of-band approve/reject of a single write
 *  operation on "MySpace". Also carries the site-building 3-way design pick
 *  (decision: 'choice' + optionId / 'skip') — both go through the same backend
 *  endpoint and the same stale/chat_interrupted invalidation contract. */
export async function confirmFileWrite(
  chatId: string,
  confirmId: string,
  decision: FileConfirmDecision | 'choice' | 'skip',
  optionId?: string,
): Promise<{
  ok: boolean;
  // Confirmation has expired (backend timeout reclaim / process restart): backend
  // returns 200 + stale. Not the user's fault — the frontend silently dismisses
  // the confirm bar plus a friendly hint, not treated as an error.
  stale?: boolean;
  // Plan F mid-term: True means this stale was caused by a server restart killing
  // the agent task, not an ordinary timeout — the user must send a new message to
  // continue. The frontend shows a prominent notice for this instead of silently
  // dismissing the bar.
  chat_interrupted?: boolean;
  message?: string;
  decision?: string;
  op?: string;
  logical_path?: string;
  cascaded?: string[];
}> {
  const wrapped = await apiRequest<unknown>(`/v1/chats/${chatId}/file-confirm`, {
    method: 'POST',
    body: JSON.stringify({
      confirm_id: confirmId,
      decision,
      ...(optionId ? { option_id: optionId } : {}),
    }),
  });
  return unwrapData(wrapped);
}

/** §13: backend shape (snake_case) → frontend FileConfirmInfo. SSE events and REST
 *  payloads share this mapping so the field contract can't drift in two places. */
export function toFileConfirmInfo(r: Record<string, unknown>): FileConfirmInfo {
  return {
    confirmId: String(r.confirm_id ?? ''),
    op: String(r.op ?? ''),
    logicalPath: String(r.logical_path ?? ''),
    message: typeof r.message === 'string' ? r.message : undefined,
    kind: typeof r.kind === 'string' ? r.kind : undefined,
  };
}

/** Site-building 3-way design pick: backend shape (snake_case) → frontend DesignPickInfo.
 *  The SSE design_pick event and pending-confirm recovery share this mapping. */
export function toDesignPickInfo(r: Record<string, unknown>): DesignPickInfo {
  const rawOpts = Array.isArray(r.options)
    ? (r.options as Record<string, unknown>[])
    : [];
  return {
    confirmId: String(r.confirm_id ?? ''),
    question: typeof r.question === 'string' ? r.question : '',
    options: rawOpts
      .map((o) => ({
        id: String(o.id ?? ''),
        title: String(o.title ?? ''),
        brief: typeof o.brief === 'string' && o.brief ? o.brief : undefined,
        imageFileId: String(o.image_file_id ?? ''),
      }))
      .filter((o) => o.id && o.imageFileId),
  };
}

/** Site-building 3-way design pick: submit the user's choice out-of-band (optionId null = let the assistant decide). */
export function submitDesignPick(
  chatId: string,
  confirmId: string,
  optionId: string | null,
) {
  return confirmFileWrite(
    chatId, confirmId, optionId ? 'choice' : 'skip', optionId ?? undefined,
  );
}

/** §13: fetch ALL pending confirmations for a single chat (restores the whole
 *  queue on refresh / tab switch-back). One round of parallel tool calls can
 *  concurrently register N distinct pending items, so all must be retrieved and
 *  queued. The result is split by kind: write-confirm queue + site-building
 *  design picker (the latter has single-value semantics — take the latest). */
export async function getPendingConfirm(
  chatId: string,
): Promise<{ confirms: FileConfirmInfo[]; designPick: DesignPickInfo | null }> {
  const wrapped = await apiRequest<{
    pendings?: Record<string, unknown>[];
  }>(`/v1/chats/${chatId}/pending-confirm`);
  const { pendings } = unwrapData<{
    pendings?: Record<string, unknown>[];
  }>(wrapped);
  const list = Array.isArray(pendings) ? pendings : [];
  const picks = list.filter((r) => String(r.kind ?? '') === 'design_pick');
  const confirms = list
    .filter((r) => String(r.kind ?? '') !== 'design_pick')
    .map(toFileConfirmInfo);
  const pick = picks.length ? toDesignPickInfo(picks[picks.length - 1]) : null;
  return { confirms, designPick: pick && pick.confirmId ? pick : null };
}

/** §13: batch-fetch all of the current user's chats that have pending
 *  confirmations (lights up the sidebar blue dot on first load / refresh).
 *  Split by kind: write confirms go into the pendingConfirm queue (kept
 *  homogeneous), design_pick goes into the single pendingDesignPick slot —
 *  both contribute to the sidebar blue dot. */
export async function listPendingConfirms(): Promise<{
  confirms: Array<{ chatId: string; info: FileConfirmInfo }>;
  designPicks: Array<{ chatId: string; info: DesignPickInfo }>;
}> {
  const wrapped = await apiRequest<{ items: Record<string, unknown>[] }>(
    '/v1/chats/pending-confirms',
  );
  const { items } = unwrapData<{ items: Record<string, unknown>[] }>(wrapped);
  const confirms: Array<{ chatId: string; info: FileConfirmInfo }> = [];
  const designPicks: Array<{ chatId: string; info: DesignPickInfo }> = [];
  for (const it of items || []) {
    const chatId = String(it.chat_id ?? '');
    if (!chatId) continue;
    if (String(it.kind ?? '') === 'design_pick') {
      const pick = toDesignPickInfo(it);
      if (pick.confirmId) designPicks.push({ chatId, info: pick });
    } else {
      confirms.push({ chatId, info: toFileConfirmInfo(it) });
    }
  }
  return { confirms, designPicks };
}

/** DSH recommendation suffix → display label plus badge state.
 * The original label stays authoritative in the backend and is what the model
 * receives in `answers[].selected`; this helper only cleans up presentation. */
export function parseRecommendedLabel(label: string): {
  label: string;
  recommended: boolean;
} {
  const suffix = /\s*(?:\((?:recommended|推荐)\)|（(?:recommended|推荐)）)\s*$/i;
  return suffix.test(label)
    ? { label: label.replace(suffix, ''), recommended: true }
    : { label, recommended: false };
}

/** Backend internal transport → resident-composer state. */
export function toUserQuestionRequest(raw: Record<string, unknown>): UserQuestionRequest {
  const rawQuestions = Array.isArray(raw.questions)
    ? raw.questions as Record<string, unknown>[]
    : [];
  return {
    requestId: String(raw.request_id ?? ''),
    createdAt: typeof raw.created_at === 'number' ? raw.created_at : undefined,
    expiresAt: typeof raw.expires_at === 'number' ? raw.expires_at : undefined,
    questions: rawQuestions
      .map((question) => {
        const rawOptions = Array.isArray(question.options)
          ? question.options as Record<string, unknown>[]
          : [];
        return {
          id: String(question.id ?? ''),
          header: typeof question.header === 'string' && question.header
            ? question.header : undefined,
          question: String(question.question ?? ''),
          description: typeof question.description === 'string' && question.description
            ? question.description : undefined,
          multiSelect: question.multi_select === true,
          options: rawOptions
            .map((option) => {
              const display = parseRecommendedLabel(String(option.label ?? ''));
              return {
                id: String(option.id ?? ''),
                label: display.label,
                description: typeof option.description === 'string' && option.description
                  ? option.description : undefined,
                recommended: option.recommended === true || display.recommended,
              };
            })
            .filter((option) => option.id && option.label),
        };
      })
      .filter((question) => question.id && question.question),
  };
}

/** Merge a pending-GET snapshot without dropping requests delivered by SSE
 * after that GET started. Resolved-event tombstones are applied by uiStore
 * during hydration, so this helper only protects the newer-arrival side. */
export function mergePendingUserQuestionRecovery(
  snapshot: UserQuestionRequest[],
  current: UserQuestionRequest[],
  requestIdsAtStart: ReadonlySet<string>,
): UserQuestionRequest[] {
  const merged = [...snapshot];
  for (const request of current) {
    if (requestIdsAtStart.has(request.requestId)) continue;
    if (!merged.some((item) => item.requestId === request.requestId)) {
      merged.push(request);
    }
  }
  return merged;
}

export async function getPendingUserQuestions(chatId: string): Promise<UserQuestionRequest[]> {
  const wrapped = await apiRequest<{ requests?: Record<string, unknown>[] }>(
    `/v1/chats/${chatId}/pending-user-questions`,
  );
  const { requests } = unwrapData<{ requests?: Record<string, unknown>[] }>(wrapped);
  return (Array.isArray(requests) ? requests : [])
    .map(toUserQuestionRequest)
    .filter((request) => request.requestId && request.questions.length);
}

export async function listPendingUserQuestions(): Promise<
  Array<{ chatId: string; request: UserQuestionRequest }>
> {
  // 这个端点不带会话 id，路由头推断不出目标。双模式下本机面与云端各自持有一份挂起
  // 问题（挂起状态是进程内的），所以两边都要问：只问云端会让本机会话的提问永远不出
  // 现在恢复轮询里，对话就一直停在「已发起提问、界面没有卡片」。
  const targets: Array<'local' | undefined> = isHybridDual() ? [undefined, 'local'] : [undefined];
  const responses = await Promise.allSettled(
    targets.map((target) =>
      apiRequest<{ items?: Record<string, unknown>[] }>(
        '/v1/chats/pending-user-questions',
        undefined,
        target,
      ),
    ),
  );
  // 调用方把返回值当成服务端权威快照，缺席的问题会被清掉——所以任一面查询失败就整体
  // 抛错，让调用方保留现有状态等下一轮，绝不能把没查到的一面当成「已经没有问题了」。
  const failed = responses.find((response) => response.status === 'rejected');
  if (failed?.status === 'rejected') throw failed.reason;
  const out: Array<{ chatId: string; request: UserQuestionRequest }> = [];
  const seen = new Set<string>();
  for (const response of responses) {
    if (response.status !== 'fulfilled') continue;
    const { items } = unwrapData<{ items?: Record<string, unknown>[] }>(response.value);
    for (const item of Array.isArray(items) ? items : []) {
      const chatId = String(item.chat_id ?? '');
      const request = toUserQuestionRequest(item);
      if (!chatId || !request.requestId || !request.questions.length) continue;
      const key = `${chatId}:${request.requestId}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ chatId, request });
    }
  }
  return out;
}

export async function answerUserQuestion(
  chatId: string,
  requestId: string,
  answers: UserQuestionAnswer[],
): Promise<{
  ok: boolean;
  outcome?: string;
  stale?: boolean;
  chat_interrupted?: boolean;
  message?: string;
}> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chats/${chatId}/user-questions/${encodeURIComponent(requestId)}/answer`,
    { method: 'POST', body: JSON.stringify({ answers }) },
  );
  return unwrapData(wrapped);
}

export async function cancelUserQuestion(
  chatId: string,
  requestId: string,
): Promise<{
  ok: boolean;
  outcome?: string;
  stale?: boolean;
  chat_interrupted?: boolean;
  message?: string;
}> {
  const wrapped = await apiRequest<unknown>(
    `/v1/chats/${chatId}/user-questions/${encodeURIComponent(requestId)}/cancel`,
    { method: 'POST' },
  );
  return unwrapData(wrapped);
}

export async function getFollowUpQuestions(
  chatId: string,
  messageId: string,
): Promise<string[]> {
  try {
    const wrapped = await apiRequest<unknown>(
      `/v1/chats/${chatId}/messages/${messageId}/followups`,
    );
    const data = unwrapData<{ follow_up_questions?: string[] }>(wrapped);
    return Array.isArray(data?.follow_up_questions) ? data.follow_up_questions : [];
  } catch {
    return [];
  }
}
