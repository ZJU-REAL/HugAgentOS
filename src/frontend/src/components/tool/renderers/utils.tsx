import React from 'react';
import type { ToolCall } from '../../../types';

export const PREVIEW_LEN = 90;

/** 证据锚点小徽章：条目自带 cite_id（后端中间件注入）时展示，让用户把
 *  正文里的 [锚文本](cite:e7) 与工具卡片里的具体条目肉眼对上。 */
export function citeTag(item: unknown): React.ReactNode {
  const cid = (item && typeof item === 'object') ? String((item as any).cite_id || '') : '';
  if (!/^e\d+$/.test(cid)) return null;
  return <span className="jx-tr-citeTag" title={`引用锚点 ${cid}`}>{cid}</span>;
}
export const preview = (s: string) => s.length > PREVIEW_LEN ? s.slice(0, PREVIEW_LEN) + '…' : s;

/** Parse tool output as JSON if possible; return the raw value otherwise. */
export function coerceOutput(raw: unknown): unknown {
  if (typeof raw !== 'string') return raw;
  try { return JSON.parse(raw); } catch { return raw; }
}

/** True if any tool in the list is still running. */
export function anyToolRunning(tools: Pick<ToolCall, 'status'>[]): boolean {
  return tools.some((t) => t.status === 'running');
}

/** Resolve display-time tool status — a still-streaming message treats 'running' as 'success'. */
export function computeEffectiveStatus(
  tool: Pick<ToolCall, 'status'>,
  isStreaming?: boolean,
): 'running' | 'success' | 'error' {
  const raw = tool.status ?? 'success';
  if (raw === 'error') return 'error';
  if (raw === 'running') return isStreaming ? 'running' : 'success';
  return 'success';
}
