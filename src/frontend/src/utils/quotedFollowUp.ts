import type { QuotedFollowUp, QuoteSource } from '../types';

type WireQuoteSource =
  | { kind: 'message'; ts: number }
  | { kind: 'file'; file_id: string; file_name: string; locator?: string };

/** `quoted_follow_up` as the chat API takes it and returns it in message metadata. */
export interface WireQuotedFollowUp {
  text: string;
  source?: WireQuoteSource;
}

export function quoteToWire({ text, source }: QuotedFollowUp): WireQuotedFollowUp {
  if (!source) return { text };
  if (source.kind === 'message') return { text, source };
  return {
    text,
    source: {
      kind: 'file', file_id: source.fileId, file_name: source.fileName,
      ...(source.locator ? { locator: source.locator } : {}),
    },
  };
}

function sourceFromWire(raw: unknown): QuoteSource | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const source = raw as Record<string, unknown>;
  if (source.kind === 'message' && typeof source.ts === 'number') return { kind: 'message', ts: source.ts };
  if (source.kind === 'file' && typeof source.file_id === 'string' && typeof source.file_name === 'string') {
    return {
      kind: 'file', fileId: source.file_id, fileName: source.file_name,
      ...(typeof source.locator === 'string' && source.locator ? { locator: source.locator } : {}),
    };
  }
  return undefined;
}

export function quoteFromWire(raw: unknown): QuotedFollowUp | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const quote = raw as Record<string, unknown>;
  if (typeof quote.text !== 'string' || !quote.text) return undefined;
  const source = sourceFromWire(quote.source);
  return source ? { text: quote.text, source } : { text: quote.text };
}

/** Rebuild an in-app quote read back from untrusted storage (a queued turn in localStorage). */
export function normalizeQuote(raw: unknown): QuotedFollowUp | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const { text, source } = raw as { text?: unknown; source?: unknown };
  if (typeof text !== 'string' || !text) return undefined;
  if (!source || typeof source !== 'object') return { text };
  const s = source as Record<string, unknown>;
  if (s.kind === 'message' && typeof s.ts === 'number') return { text, source: { kind: 'message', ts: s.ts } };
  if (s.kind === 'file' && typeof s.fileId === 'string' && typeof s.fileName === 'string') {
    return { text, source: { kind: 'file', fileId: s.fileId, fileName: s.fileName, ...(typeof s.locator === 'string' && s.locator ? { locator: s.locator } : {}) } };
  }
  return { text };
}

/** "报告.docx · 第 3 页" — how a file quote names where it came from. */
export function quoteSourceLabel(source: QuoteSource | undefined): string | null {
  if (source?.kind !== 'file') return null;
  return source.locator ? `${source.fileName} · ${source.locator}` : source.fileName;
}
