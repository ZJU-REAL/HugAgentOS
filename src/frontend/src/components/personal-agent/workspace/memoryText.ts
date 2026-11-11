/**
 * Reading memory wiki pages in the browser: frontmatter, body blocks and the inline marks of a
 * fact line ([src: … | as-of: …], [[slug]], "was X until <date>", （agent 观察）, CONFLICT:).
 * Mirrors personal_agent/memory/page.py; parsing stays line based and forgiving.
 */
import { ApiResponseError } from '../../../utils/apiError';
import type { MemoryLintFinding } from '../api';

export interface PageMeta { name?: string; description?: string; aliases: string[]; type?: string; status?: string; updated?: string }
export interface ParsedPage { meta: PageMeta; body: string; hasFrontmatter: boolean }

export function parsePage(text: string): ParsedPage {
  const empty: PageMeta = { aliases: [] };
  if (!text.startsWith('---\n')) return { meta: empty, body: text, hasFrontmatter: false };
  const end = text.indexOf('\n---', 4);
  if (end < 0) return { meta: empty, body: text, hasFrontmatter: false };
  const meta: PageMeta = { aliases: [] };
  for (const line of text.slice(4, end).split('\n')) {
    const at = line.indexOf(':');
    if (at < 0) continue;
    const key = line.slice(0, at).trim();
    const value = line.slice(at + 1).trim();
    if (key === 'aliases') {
      try { const list: unknown = JSON.parse(value); if (Array.isArray(list)) meta.aliases = list.filter((a): a is string => typeof a === 'string'); }
      catch { meta.aliases = []; }
    } else if (key === 'name' || key === 'description' || key === 'type' || key === 'status' || key === 'updated') {
      meta[key] = value;
    }
  }
  const rest = text.slice(end + 4);
  return { meta, body: rest.startsWith('\n') ? rest.slice(1) : rest, hasFrontmatter: true };
}

export type Block =
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'fact'; text: string; conflict: boolean; depth: number }
  | { kind: 'text'; text: string }
  | { kind: 'stub'; target: string };

const STUB = /^\s*MOVED TO \[\[([a-z0-9-]+)\]\]\s*$/;

export function blocks(body: string): Block[] {
  const lines = body.split('\n').filter((line) => line.trim());
  if (lines.length === 1) {
    const stub = STUB.exec(lines[0]);
    if (stub) return [{ kind: 'stub', target: stub[1] }];
  }
  const out: Block[] = [];
  for (const line of body.split('\n')) {
    if (!line.trim()) continue;
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) { out.push({ kind: 'heading', level: heading[1].length, text: heading[2] }); continue; }
    const item = /^(\s*)[-*]\s+(.*)$/.exec(line);
    if (item) {
      const text = item[2];
      out.push({ kind: 'fact', text, conflict: text.startsWith('CONFLICT:'), depth: Math.min(3, Math.floor(item[1].length / 2)) });
      continue;
    }
    out.push({ kind: 'text', text: line });
  }
  return out;
}

export type SourceKind = 'conv' | 'chan' | 'task';
export type Inline =
  | { kind: 'text'; text: string }
  | { kind: 'strong'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'source'; id: string; sourceKind: SourceKind | null; asOf: string }
  | { kind: 'link'; slug: string }
  | { kind: 'superseded'; value: string; until: string }
  | { kind: 'observation'; text: string };

// One pass over the line; the first alternative that matches at a position wins.
const INLINE = new RegExp([
  /\[src:\s*([^\]|]+?)\s*\|\s*as-of:\s*(\d{4}-\d{2}-\d{2})\s*\]/.source,   // 1, 2
  /\[\[([a-z0-9-]+)\]\]/.source,                                             // 3
  /\bwas\s+(.+?)\s+until\s+(\d{4}-\d{2}-\d{2})/.source,                       // 4, 5
  /([（(]agent\s*观察[)）])/.source,                                          // 6
  /\*\*([^*]+)\*\*/.source,                                                  // 7
  /`([^`]+)`/.source,                                                        // 8
].join('|'), 'g');

export function sourceKind(id: string): SourceKind | null {
  const match = /^(conv|chan|task)-/.exec(id);
  return match ? (match[1] as SourceKind) : null;
}

export function inline(text: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    const at = match.index ?? 0;
    if (at > last) out.push({ kind: 'text', text: text.slice(last, at) });
    if (match[1] !== undefined) out.push({ kind: 'source', id: match[1], sourceKind: sourceKind(match[1]), asOf: match[2] });
    else if (match[3] !== undefined) out.push({ kind: 'link', slug: match[3] });
    else if (match[4] !== undefined) out.push({ kind: 'superseded', value: match[4], until: match[5] });
    else if (match[6] !== undefined) out.push({ kind: 'observation', text: match[6] });
    else if (match[7] !== undefined) out.push({ kind: 'strong', text: match[7] });
    else if (match[8] !== undefined) out.push({ kind: 'code', text: match[8] });
    last = at + match[0].length;
  }
  if (last < text.length) out.push({ kind: 'text', text: text.slice(last) });
  return out;
}

/** The slug of /workspace/memory/<slug>.md, or null for any other path. */
export function slugOf(path: string): string | null {
  const match = /(?:^|\/)memory\/([a-z0-9-]+)\.md$/.exec(path);
  return match && !match[1].startsWith('_') ? match[1] : null;
}

export type DiffLine = { kind: 'same' | 'add' | 'del'; text: string };

/** A line diff (LCS). Large inputs fall back to "all removed, all added" to stay responsive. */
export function lineDiff(before: string, after: string): DiffLine[] {
  const a = before ? before.split('\n') : [];
  const b = after ? after.split('\n') : [];
  if (a.length * b.length > 400_000) {
    return [...a.map((text): DiffLine => ({ kind: 'del', text })), ...b.map((text): DiffLine => ({ kind: 'add', text }))];
  }
  const rows = a.length + 1;
  const cols = b.length + 1;
  const table = new Uint32Array(rows * cols);
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i * cols + j] = a[i] === b[j] ? table[(i + 1) * cols + j + 1] + 1 : Math.max(table[(i + 1) * cols + j], table[i * cols + j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { out.push({ kind: 'same', text: a[i] }); i += 1; j += 1; }
    else if (table[(i + 1) * cols + j] >= table[i * cols + j + 1]) { out.push({ kind: 'del', text: a[i] }); i += 1; }
    else { out.push({ kind: 'add', text: b[j] }); j += 1; }
  }
  while (i < a.length) { out.push({ kind: 'del', text: a[i] }); i += 1; }
  while (j < b.length) { out.push({ kind: 'add', text: b[j] }); j += 1; }
  return out;
}

/** Lint findings a refused save carries (400 from the memory store). */
export function lintFindings(exc: unknown): MemoryLintFinding[] {
  if (!(exc instanceof ApiResponseError)) return [];
  const data = exc.data;
  if (!data || typeof data !== 'object' || !('findings' in data)) return [];
  const findings = (data as { findings: unknown }).findings;
  return Array.isArray(findings)
    ? findings.filter((f): f is MemoryLintFinding => !!f && typeof f === 'object' && 'detail' in f && 'rule' in f)
    : [];
}

export function isConflict(exc: unknown): boolean {
  return exc instanceof ApiResponseError && exc.status === 409;
}

/**
 * The newest report in DREAMS.md. Reports are sections under a level-1 or level-2 heading;
 * the one whose heading carries the latest date wins, else the first one.
 */
export function latestReport(text: string): { report: string; more: boolean } {
  const lines = text.split('\n');
  const level = lines.some((line) => /^# /.test(line)) && lines.filter((line) => /^# /.test(line)).length > 1 ? '# ' : '## ';
  const sections: string[][] = [];
  let head: string[] = [];
  for (const line of lines) {
    if (line.startsWith(level)) sections.push([line]);
    else if (sections.length) sections[sections.length - 1].push(line);
    else head.push(line);
  }
  if (sections.length <= 1) return { report: text, more: false };
  const dated = sections.map((section, index) => ({ section, index, date: /\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?/.exec(section[0])?.[0] ?? '' }));
  const best = dated.reduce((a, b) => (b.date > a.date ? b : a), dated[0]);
  head = head.filter((line) => line.trim() && !/^# /.test(line));
  return { report: [...head, ...best.section].join('\n'), more: true };
}
