import React from 'react';
import type { ToolCall } from '../../../types';
import { unwrapMcpResult } from '../../../utils/mcpResult';

export { unwrapMcpResult };

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
  if (typeof raw !== 'string') return unwrapMcpResult(raw);
  try { return unwrapMcpResult(JSON.parse(raw)); } catch { return raw; }
}

/** True if any tool in the list is still running. */
export function anyToolRunning(tools: Pick<ToolCall, 'status'>[]): boolean {
  return tools.some((t) => t.status === 'running');
}

/** Resolve display-time tool status — a still-streaming message treats 'running' as 'success'.
 *
 *  中断是它自己的一档，不能并进 success：流收尾时没拿到结果的调用会被标成
 *  interrupted（见 hooks/chatStream.ts），在这里再抹平一次，用户看到的就还是一张
 *  "成功但没有输出"的卡，异常照样隐形。 */
export function computeEffectiveStatus(
  tool: Pick<ToolCall, 'status'>,
  isStreaming?: boolean,
): 'running' | 'success' | 'error' | 'interrupted' {
  const raw = tool.status ?? 'success';
  if (raw === 'error') return 'error';
  if (raw === 'interrupted') return 'interrupted';
  if (raw === 'running') return isStreaming ? 'running' : 'success';
  return 'success';
}
