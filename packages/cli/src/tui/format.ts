/** Pure formatting helpers shared by the transcript, footer and prompts. */

/** Keys whose value best says what a tool call is about, in order of preference. */
const SUMMARY_KEYS = [
  'command', 'cmd', 'path', 'file_path', 'filepath', 'file', 'filename', 'query', 'q', 'url', 'pattern',
  'skill', 'name', 'prompt', 'task', 'description', 'title', 'content',
];

export function oneLine(text: string, max = 80): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > max ? flat.slice(0, max - 1) + '…' : flat;
}

/** Parse tool arguments that may still be a partial JSON string while they stream. */
export function parseArgs(input: unknown): unknown {
  if (typeof input !== 'string') return input;
  const text = input.trim();
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    // Partial JSON: pull the first complete "key": "value" pair so the line shows something useful.
    const pairs = [...text.matchAll(/"([\w-]+)"\s*:\s*"((?:[^"\\]|\\.)*)"?/g)];
    if (pairs.length) {
      const obj: Record<string, string> = {};
      for (const [, key, value] of pairs) obj[key] = value.replace(/\\n/g, ' ').replace(/\\(.)/g, '$1');
      return obj;
    }
    return text;
  }
}

/** Short human summary of a tool call's arguments: the most telling value, flattened to one line. */
export function summarizeArgs(input: unknown, max = 60): string {
  const value = parseArgs(input);
  if (value == null) return '';
  if (typeof value === 'string') return oneLine(value, max);
  if (typeof value !== 'object') return oneLine(String(value), max);
  if (Array.isArray(value)) return value.length ? `${value.length} 项` : '';
  const obj = value as Record<string, unknown>;
  for (const key of SUMMARY_KEYS) {
    const v = obj[key];
    if (typeof v === 'string' && v.trim()) return oneLine(v, max);
    if (Array.isArray(v) && v.length && typeof v[0] === 'string') return oneLine(v.join(' '), max);
  }
  for (const v of Object.values(obj)) {
    if (typeof v === 'string' && v.trim()) return oneLine(v, max);
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  }
  return '';
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function outputText(output: unknown): string {
  if (output == null) return '';
  if (typeof output === 'string') return output;
  try {
    return JSON.stringify(output, null, 2);
  } catch {
    return String(output);
  }
}

/** "12 行" for multi-line text output, otherwise its size. Empty output gives ''. */
export function resultSize(output: unknown): string {
  if (output == null || output === '') return '';
  if (typeof output === 'string') {
    const lines = output.split('\n').length;
    return lines > 1 ? `${lines} 行` : formatBytes(Buffer.byteLength(output));
  }
  let compact: string;
  try { compact = JSON.stringify(output) ?? ''; } catch { compact = String(output); }
  return formatBytes(Buffer.byteLength(compact));
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m${Math.round(s - m * 60)}s`;
}

export function formatTokens(count: number): string {
  if (count < 1000) return String(count);
  if (count < 10_000) return `${(count / 1000).toFixed(1)}k`;
  if (count < 1_000_000) return `${Math.round(count / 1000)}k`;
  return `${(count / 1_000_000).toFixed(1)}M`;
}

/** Context fill in percent from a `context_usage` payload (event or `meta.context_usage`). */
export function contextPercent(raw: unknown): number | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const r = raw as Record<string, unknown>;
  const used = Number(r.used_tokens);
  const window = Number(r.context_window);
  if (!Number.isFinite(used) || !Number.isFinite(window) || window <= 0) return undefined;
  return Math.min(100, Math.max(0, (used / window) * 100));
}


export function str(value: unknown): string {
  return typeof value === 'string' ? value : value == null ? '' : String(value);
}
