import type { ChatMessage, MessageSegment } from '../types';

export type CompactionProgress = {
  id: string;
  chat_id: string;
  run_id: string;
  message_id?: string;
  status: 'running' | 'completed' | 'failed' | 'interrupted' | 'skipped';
  phase: string;
  reason?: string;
  started_at: number;
  ended_at?: number;
  expires_at?: number;
};

export function parseCompactionProgress(raw: unknown): CompactionProgress | null {
  if (!raw || typeof raw !== 'object') return null;
  const value = raw as Record<string, unknown>;
  if (typeof value.id !== 'string' || !value.id
    || typeof value.chat_id !== 'string' || typeof value.run_id !== 'string'
    || typeof value.started_at !== 'number' || !Number.isFinite(value.started_at)
    || typeof value.status !== 'string'
    || !['running', 'completed', 'failed', 'interrupted', 'skipped'].includes(value.status)) return null;
  const expired = value.status === 'running' && typeof value.expires_at === 'number' && value.expires_at <= Date.now();
  return {
    id: value.id, chat_id: value.chat_id, run_id: value.run_id,
    message_id: typeof value.message_id === 'string' ? value.message_id : undefined,
    phase: typeof value.phase === 'string' ? value.phase : '',
    status: expired ? 'interrupted' : value.status as CompactionProgress['status'],
    reason: expired ? 'expired' : typeof value.reason === 'string' ? value.reason : undefined,
    started_at: value.started_at,
    ended_at: typeof value.ended_at === 'number' && Number.isFinite(value.ended_at) ? value.ended_at : undefined,
    expires_at: typeof value.expires_at === 'number' && Number.isFinite(value.expires_at) ? value.expires_at : undefined,
  };
}

/** Upsert a system step, preserving its original place and terminal status on replay. */
export function upsertCompactionSegment(segments: MessageSegment[], progress: CompactionProgress): MessageSegment[] {
  const index = segments.findIndex(s => s.type === 'compaction' && s.compaction?.id === progress.id);
  const next = [...segments];
  if (index >= 0) {
    const previous = next[index].compaction;
    if (progress.reason === 'expired' && previous?.status !== 'running' && previous?.reason !== 'expired') return segments;
    if (next[index].compaction?.status !== 'running' && progress.status === 'running') return segments;
    next[index] = { type: 'compaction', compaction: progress };
  } else {
    let anchor = next.length;
    if (progress.phase === 'post_turn') {
      const lastTool = next.map(s => s.type).lastIndexOf('tool');
      anchor = lastTool >= 0 ? lastTool + 1 : Math.max(0, next.findIndex(s => s.type === 'text'));
    }
    next.splice(anchor, 0, { type: 'compaction', compaction: progress });
  }
  return next;
}

export function mergeCompactionProgress(messages: ChatMessage[], chatId: string, raw: unknown): ChatMessage[] {
  if (!Array.isArray(raw)) return messages;
  const events = raw.map(parseCompactionProgress).filter((p): p is CompactionProgress => !!p && p.chat_id === chatId);
  return messages.map(message => {
    const own = events.filter(event => !!event.message_id && event.message_id === message.messageId);
    if (!own.length || message.role !== 'assistant') return message;
    let segments = message.segments || [
      ...(message.toolCalls || []).map((_, toolIndex) => ({ type: 'tool' as const, toolIndex })),
      ...(message.content ? [{ type: 'text' as const, content: message.content }] : []),
    ];
    for (const progress of own) segments = upsertCompactionSegment(segments, progress);
    return { ...message, segments };
  });
}

export function compactionLabel(progress: CompactionProgress): string {
  if (progress.status === 'running') return '正在压缩上下文';
  if (progress.status === 'completed') return '上下文已压缩';
  if (progress.status === 'interrupted') return progress.reason === 'expired' ? '压缩状态已过期' : '上下文压缩已中断';
  if (progress.status === 'failed') return '上下文压缩失败';
  if (progress.reason === 'busy') return '已有上下文压缩任务正在执行';
  if (progress.reason === 'disabled') return '上下文压缩未启用';
  return '当前上下文无需压缩';
}
