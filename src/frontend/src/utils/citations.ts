import type { CitationItem, ToolCall, ChatMessage } from '../types';
import { t } from '../i18n';

/**
 * 引用标记的唯一真源正则（三种形态并行识别）：
 *   [ref:tool-N]        旧格式（历史消息）   → group 1
 *   [锚文本](cite:eN)   证据锚点主格式       → group 2(锚文本) + 3(锚点 id)
 *   [[eN]]              obsidian 双链容错    → group 4
 *
 * 容错点（都实际出现过，漏一个就整条标记原样漏进正文，用户看到的是
 * `[来源](cite:e8)` 这样的裸标记）：
 *   - `cite:` 后允许空白；
 *   - 锚点前缀 `e` 允许大写、也允许模型直接写成 `cite:8`；
 *   - 整体大小写不敏感。
 *
 * 做成工厂而不是共享常量：带 /g 的正则实例会把 lastIndex 带到下一次调用，
 * 共享时 test() 与 replace() 会互相污染、漏匹配。
 */
export const citationMarkerRe = () =>
  /\[ref:([\w]+-\d+)\]|\[([^[\]]*)\]\(\s*cite:\s*e?(\d+)\s*\)|\[\[\s*e?(\d+)\s*\]\]/gi;

/** 把正则捕获到的锚点数字统一还原成 `eN` —— citations[].id 用的就是这个形态。 */
export function normalizeAnchorId(digits: string | undefined): string {
  return digits ? `e${digits}` : '';
}

/** 从一次匹配里取出「锚点 id + 锚文本」。 */
export function citationMarkerParts(
  m: RegExpExecArray | RegExpMatchArray,
): { id: string; label: string } {
  if (m[1]) return { id: m[1], label: '' };
  if (m[3]) return { id: normalizeAnchorId(m[3]), label: (m[2] || '').trim() };
  return { id: normalizeAnchorId(m[4]), label: '' };
}

export function getCitationItemIndex(citationId: string, citation?: CitationItem): number {
  // 证据锚点引用自带精确下标（后端发号时记录），优先使用
  if (citation && typeof citation.item_index === 'number' && citation.item_index >= 0) {
    return citation.item_index;
  }
  // 旧格式 "tool-N"：末段序号转 0-based；锚点 id（"e7"）无旧序号语义 → 0
  const idx = Number(citationId.split('-').pop() || '1');
  return Number.isInteger(idx) && idx > 0 ? idx - 1 : 0;
}

function normalizeMaybeId(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const id = value.trim();
  return id.length > 0 ? id : undefined;
}

function coerceToolOutput(raw: unknown): unknown {
  if (typeof raw !== 'string') return raw;
  try { return JSON.parse(raw); } catch { return raw; }
}

function getFallbackCitationOutput(citation: CitationItem): { toolName: string; output: unknown } {
  switch (citation.tool_name) {
    case 'internet_search':
      return {
        toolName: 'internet_search',
        output: {
          result: {
            results: [{
              title: citation.title,
              url: citation.url,
              content: citation.snippet,
            }],
          },
        },
      };
    case 'retrieve_dataset_content':
      return {
        toolName: 'retrieve_dataset_content',
        output: {
          items: [{
            文件名称: citation.title,
            文件内容: citation.snippet,
          }],
        },
      };
    case 'retrieve_local_kb':
      return {
        toolName: 'retrieve_local_kb',
        output: {
          items: [{
            title: citation.title,
            content: citation.snippet,
          }],
        },
      };
    default:
      return {
        toolName: citation.tool_name,
        output: citation.snippet || t('暂无内容'),
      };
  }
}

export function getCitationOutputSlice(
  citation: CitationItem,
  toolCalls?: ChatMessage['toolCalls']
): { toolName: string; output: unknown } {
  const citationIndex = getCitationItemIndex(citation.id, citation);
  const citationToolId = normalizeMaybeId(citation.tool_id);

  const targetTool = (
    citationToolId
      ? toolCalls?.find((tool) => normalizeMaybeId(tool.id) === citationToolId)
      : undefined
  ) || (() => {
    if (!toolCalls) return undefined;
    let lastMatch: ToolCall | undefined;
    for (const tool of toolCalls) {
      if (tool.name === citation.tool_name && tool.output != null) {
        lastMatch = tool;
      }
    }
    return lastMatch;
  })();

  if (!targetTool || targetTool.output == null) {
    return getFallbackCitationOutput(citation);
  }

  const parsed = coerceToolOutput(targetTool.output);

  if (citation.tool_name === 'internet_search') {
    const data = (typeof parsed === 'object' && parsed !== null ? parsed : {}) as any;
    const searchResult = data?.result ?? data;
    const results: any[] = Array.isArray(searchResult?.results) ? searchResult.results : [];
    const picked = results[citationIndex] ?? results[0];
    const compactSearchResult = {
      ...(typeof searchResult === 'object' && searchResult !== null ? searchResult : {}),
      results: picked ? [picked] : [],
    };
    if ('result' in data) {
      return { toolName: 'internet_search', output: { ...data, result: compactSearchResult } };
    }
    return { toolName: 'internet_search', output: compactSearchResult };
  }

  // Any list-shaped result (host tool or plugin tool alike) narrows to the one
  // cited entry, so the card opened from a citation shows that row rather than
  // the whole batch. Keyed on the payload's shape, not on a tool name — a
  // plugin's list tool gets the same behaviour without being enumerated here.
  const listShaped = (typeof parsed === 'object' && parsed !== null
    && Array.isArray((parsed as any).items)) as boolean;
  if (listShaped) {
    const data = parsed as any;
    const items: any[] = data.items;
    const picked = items[citationIndex] ?? items[0];
    return {
      toolName: citation.tool_name,
      output: {
        ...data,
        items: picked ? [picked] : [],
      },
    };
  }

  if (citation.tool_name === 'retrieve_local_kb') {
    const data = (typeof parsed === 'object' && parsed !== null ? parsed : {}) as any;
    const items: any[] = Array.isArray(data?.items) ? data.items : Array.isArray(data) ? data : [];
    const picked = items[citationIndex] ?? items[0];
    return {
      toolName: 'retrieve_local_kb',
      output: {
        items: picked ? [picked] : [],
      },
    };
  }

  return { toolName: citation.tool_name, output: parsed };
}

/**
 * resolveConversationCitations: resolve [ref:xxx-N] markers that reference
 * citations from PREVIOUS messages in the conversation (cross-turn references).
 *
 * When the LLM generates a response referencing a previous turn's tool results,
 * the current message's citations array won't contain those references.
 * This function looks up unmatched markers in earlier messages' citations.
 */
export function resolveConversationCitations(
  text: string,
  messageCitations: CitationItem[],
  allMessages: Array<{ ts: number; citations?: CitationItem[] }>,
  currentTs: number,
): CitationItem[] {
  if (!text) return messageCitations;

  // Find all citation markers in the text (格式见 citationMarkerRe)
  const markerPattern = citationMarkerRe();
  const referencedIds = new Set<string>();
  let match: RegExpExecArray | null;
  while ((match = markerPattern.exec(text)) !== null) {
    const { id } = citationMarkerParts(match);
    if (id) referencedIds.add(id);
  }

  if (referencedIds.size === 0) return messageCitations;

  // Check which are already covered by current message's citations
  const currentIds = new Set(messageCitations.map(c => c.id));
  const missingIds = new Set<string>();
  for (const id of referencedIds) {
    if (!currentIds.has(id)) missingIds.add(id);
  }

  if (missingIds.size === 0) return messageCitations;

  // Search previous messages (reverse order → most recent first)
  const extraCitations: CitationItem[] = [];
  const foundIds = new Set<string>();

  for (let i = allMessages.length - 1; i >= 0; i--) {
    const msg = allMessages[i];
    if (msg.ts === currentTs || !msg.citations) continue;

    for (const cit of msg.citations) {
      if (missingIds.has(cit.id) && !foundIds.has(cit.id)) {
        extraCitations.push(cit);
        foundIds.add(cit.id);
      }
    }

    if (foundIds.size === missingIds.size) break;
  }

  if (extraCitations.length === 0) return messageCitations;
  return [...messageCitations, ...extraCitations];
}

export { coerceToolOutput, normalizeMaybeId };
