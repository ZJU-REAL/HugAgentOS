import type { CitationItem, ToolCall, ChatMessage } from '../types';
import { t } from '../i18n';

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

  if (citation.tool_name === 'retrieve_dataset_content') {
    const data = (typeof parsed === 'object' && parsed !== null ? parsed : {}) as any;
    const items: any[] = Array.isArray(data?.items) ? data.items : [];
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

  // Find all citation markers in the text:
  // 旧格式 [ref:tool-N] + 证据锚点 [锚文本](cite:eN) + 双链容错 [[eN]]
  const markerPattern = /\[ref:([\w]+-\d+)\]|\[[^[\]]*\]\(cite:(e\d+)\)|\[\[(e\d+)\]\]/g;
  const referencedIds = new Set<string>();
  let match: RegExpExecArray | null;
  while ((match = markerPattern.exec(text)) !== null) {
    referencedIds.add(match[1] || match[2] || match[3]);
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
