import { t } from '../i18n';
import { apiRequest, chatTargetHeaders, unwrapData } from './core';

/** 满意 / 需修改 / 不满意 */
export type EvaluationVerdict = 'adopt' | 'revise' | 'reject';
/** Main problems offered for 需修改 / 不满意, in display order. */
export type EvaluationReason = 'accuracy' | 'source' | 'calculation' | 'definition' | 'intent' | 'efficiency';
/** Retired reasons: no longer offered, still shown on records that carry them. */
export type LegacyEvaluationReason = 'completeness' | 'actionability';
export type StoredEvaluationReason = EvaluationReason | LegacyEvaluationReason;

/** Labels shared by the chat card and the task monitor (one place to add or retire an option). */
export const EVALUATION_VERDICT_LABEL: Record<EvaluationVerdict, string> = {
  adopt: t('满意'),
  revise: t('需修改'),
  reject: t('不满意'),
};

const CURRENT_REASON_LABEL: Record<EvaluationReason, string> = {
  accuracy: t('模型回答不准确'),
  source: t('数据获取来源不准确'),
  calculation: t('数据统计计算错误'),
  definition: t('数据口径出错'),
  intent: t('模型误解用户意图'),
  efficiency: t('模型回答效率低'),
};

export const EVALUATION_REASON_LABEL: Record<StoredEvaluationReason, string> = {
  ...CURRENT_REASON_LABEL,
  // Retired options: still named on earlier evaluations.
  completeness: t('完整性'),
  actionability: t('可操作性'),
};

export const EVALUATION_REASONS = Object.keys(CURRENT_REASON_LABEL) as EvaluationReason[];

export function isCurrentReason(reason: string): reason is EvaluationReason {
  return reason in CURRENT_REASON_LABEL;
}

export interface EvaluationRecord {
  verdict: EvaluationVerdict;
  reasons: StoredEvaluationReason[];
  comment: string;
}

export interface PendingEvaluationItem {
  chat_id: string;
  title: string;
  /** The conversation's latest answer still waiting for an evaluation. */
  message_id: string;
  delivered_at: string | null;
  unrated: number;
}

export interface PendingEvaluations {
  items: PendingEvaluationItem[];
  chat_ids: string[];
  total: number;
}


function toRecord(raw: unknown): EvaluationRecord | null {
  if (!raw || typeof raw !== 'object') return null;
  const value = raw as Record<string, unknown>;
  if (typeof value.verdict !== 'string' || !(value.verdict in EVALUATION_VERDICT_LABEL)) return null;
  const reasons = Array.isArray(value.reasons)
    ? value.reasons.filter((r): r is StoredEvaluationReason => typeof r === 'string' && r in EVALUATION_REASON_LABEL)
    : [];
  return {
    verdict: value.verdict as EvaluationVerdict,
    reasons,
    comment: typeof value.comment === 'string' ? value.comment : '',
  };
}

export async function submitEvaluation(
  messageId: string,
  chatId: string,
  record: EvaluationRecord,
): Promise<EvaluationRecord> {
  const payload = await apiRequest<unknown>(`/v1/chats/messages/${encodeURIComponent(messageId)}/feedback`, {
    method: 'POST',
    headers: chatTargetHeaders(chatId),
    body: JSON.stringify({
      rating: record.verdict,
      reasons: record.reasons.filter(isCurrentReason),
      comment: record.comment.trim() || undefined,
    }),
  });
  // The response is the stored record (the server applies the rules: adopt clears reasons, …).
  return toRecord(payload) ?? record;
}

export interface ChatEvaluations {
  /** This user's evaluations in the chat, keyed by message id. */
  records: Record<string, EvaluationRecord>;
  /** Answers from scheduled or loop tasks: they have no evaluation. */
  excluded: string[];
}

export async function getChatEvaluations(chatId: string, signal?: AbortSignal): Promise<ChatEvaluations> {
  const payload = await apiRequest<unknown>(`/v1/chats/${encodeURIComponent(chatId)}/feedback`, {
    signal, headers: chatTargetHeaders(chatId),
  });
  const data = unwrapData<{ items?: Record<string, unknown>; excluded?: unknown }>(payload);
  const records: Record<string, EvaluationRecord> = {};
  for (const [messageId, raw] of Object.entries(data.items ?? {})) {
    const record = toRecord(raw);
    if (record) records[messageId] = record;
  }
  const excluded = Array.isArray(data.excluded) ? data.excluded.filter((id): id is string => typeof id === 'string') : [];
  return { records, excluded };
}

export async function getPendingEvaluations(signal?: AbortSignal): Promise<PendingEvaluations> {
  const payload = await apiRequest<unknown>('/v1/chats/evaluations/pending', { signal }, 'cloud');
  const data = unwrapData<Partial<PendingEvaluations>>(payload);
  return {
    items: Array.isArray(data.items) ? data.items : [],
    chat_ids: Array.isArray(data.chat_ids) ? data.chat_ids : [],
    total: typeof data.total === 'number' ? data.total : 0,
  };
}
