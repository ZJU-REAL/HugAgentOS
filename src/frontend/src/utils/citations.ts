import type { CitationItem, MessageSegment, ToolCall, ChatMessage } from '../types';
import { t } from '../i18n';

/**
 * getContextualCitations: disambiguate duplicate citation IDs when the same tool
 * is called multiple times in one turn.
 *
 * For a text segment at position `segIdx` in the segments array, this function
 * determines which preceding tool call is the most relevant for each tool name,
 * then picks the citation from that specific call (matched via tool_id).
 *
 * When segments are unavailable (legacy rendering path), returns all citations as-is.
 */
export function getContextualCitations(
  allCitations: CitationItem[],
  segments: MessageSegment[] | undefined,
  toolCalls: ToolCall[] | undefined,
  currentSegIdx: number,
): CitationItem[] {
  if (!allCitations || allCitations.length === 0) return [];
  if (!segments || !toolCalls || segments.length === 0) return allCitations;

  const idSet = new Set<string>();
  let hasDuplicates = false;
  for (const c of allCitations) {
    if (idSet.has(c.id)) { hasDuplicates = true; break; }
    idSet.add(c.id);
  }
  if (!hasDuplicates) return allCitations;

  const latestToolIdByName = new Map<string, string>();
  for (let i = currentSegIdx - 1; i >= 0; i--) {
    const seg = segments[i];
    if (seg.type === 'tool' && seg.toolIndex != null) {
      const tc = toolCalls[seg.toolIndex];
      if (tc && tc.id && !latestToolIdByName.has(tc.name)) {
        latestToolIdByName.set(tc.name, tc.id);
      }
    }
  }

  const citationGroups = new Map<string, CitationItem[]>();
  for (const cit of allCitations) {
    const group = citationGroups.get(cit.id) || [];
    group.push(cit);
    citationGroups.set(cit.id, group);
  }

  const result: CitationItem[] = [];
  for (const [, group] of citationGroups) {
    if (group.length === 1) {
      result.push(group[0]);
      continue;
    }
    const toolName = group[0].tool_name;
    const latestToolId = latestToolIdByName.get(toolName);
    const match = latestToolId
      ? group.find(c => c.tool_id === latestToolId)
      : undefined;
    result.push(match || group[group.length - 1]);
  }

  return result;
}

export function getCitationItemIndex(citationId: string): number {
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
    case 'get_industry_news':
    case 'get_latest_ai_news':
      return {
        toolName: citation.tool_name,
        output: {
          items: [{
            标题: citation.title,
            摘要: citation.snippet,
            链接: citation.url,
            url: citation.url,
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
  const citationIndex = getCitationItemIndex(citation.id);
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

  if (citation.tool_name === 'retrieve_dataset_content' || citation.tool_name === 'get_industry_news' || citation.tool_name === 'get_latest_ai_news') {
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

  // Find all [ref:xxx-N] markers in the text
  const markerPattern = /\[ref:([\w]+-\d+)\]/g;
  const referencedIds = new Set<string>();
  let match: RegExpExecArray | null;
  while ((match = markerPattern.exec(text)) !== null) {
    referencedIds.add(match[1]);
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
