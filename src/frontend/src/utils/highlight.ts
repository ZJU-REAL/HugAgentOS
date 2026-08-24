import React from 'react';

/** Highlight all occurrences of `keyword` in `text` (case-insensitive). */
export function highlightKeyword(text: string, keyword: string): React.ReactNode {
  if (!keyword.trim()) return text;
  const escaped = keyword.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const regex = new RegExp(`(${escaped})`, 'gi');
  const parts = text.split(regex);
  if (parts.length <= 1) return text;
  return parts.map((part, i) =>
    regex.test(part)
      ? React.createElement('span', { key: i, className: 'jx-searchHighlight' }, part)
      : part
  );
}

// ── 引用片段定位（"引用的是长内容的最后一段，卡片却只显示开头"） ──────────────
//
// 引用条目里的 snippet 是后端截断过的证据片段（citation_anchor.py 按 _SNIPPET_MAX
// 截，尾部可能带省略号）。要在完整正文 / JSON 里把它找回来，不能整段 indexOf——
// 换行、连续空白、尾部省略都会让精确匹配落空。这里两步走：
//   1. 定位起点：归一化后取前缀探针，由长到短直到命中（空白宽松）；
//   2. 量出终点：从起点沿正文与片段**同步往后贴**，把整段引用都圈进来。
// 只有第 1 步（老实现）会把探针长度当成高亮长度——于是几千字的引用只涂前 200 字。

const ELLIPSIS_TAIL = /(?:…|\.{3,})+\s*$/;

/** 去掉尾部省略号并把连续空白压成单空格，得到可用于检索的"针"。 */
export function normalizeCitedFragment(raw: string): string {
  if (!raw) return '';
  return raw.replace(ELLIPSIS_TAIL, '').replace(/\s+/g, ' ').trim();
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const isWs = (ch: string) => (
  ch === ' ' || ch === '\n' || ch === '\r' || ch === '\t' || ch === '\f' || ch === '\v'
);

/**
 * 从 `start` 起，把归一化后的 `needle` 沿 `haystack` 往后贴，返回贴到哪儿、贴了多少。
 *
 * 正文里的换行 / 缩进在针里被压成了单空格，所以两边的空白各自跳过、不参与比对；
 * 遇到真正的字符不一致就停，`end` 停在最后一个对上的字符之后（不带尾随空白）。
 */
function extendMatch(haystack: string, start: number, needle: string): { end: number; consumed: number } {
  let i = start;
  let j = 0;
  let end = start;
  let consumed = 0;
  while (i < haystack.length && j < needle.length) {
    const a = haystack[i];
    const b = needle[j];
    if (isWs(a)) { i += 1; continue; }
    if (isWs(b)) { j += 1; continue; }
    if (a !== b) break;
    i += 1;
    j += 1;
    end = i;
    consumed = j;
  }
  return { end, consumed };
}

/** 空白宽松地找 `probe`：片段里的空格可能对应原文里的换行 / 缩进。 */
function looseIndexOf(haystack: string, probe: string, from = 0): number {
  const direct = haystack.indexOf(probe, from);
  if (direct >= 0) return direct;
  try {
    const re = new RegExp(probe.trim().split(/\s+/).map(escapeRe).join('\\s+'), 'g');
    re.lastIndex = from;
    const m = re.exec(haystack);
    if (m && m[0]) return m.index;
  } catch { /* 正则构造失败（片段过长）→ 交给上层缩短探针 */ }
  return -1;
}

/** 收集 probe 在 haystack 里的前若干个落点（含空白宽松的那一个）。 */
function candidateStarts(haystack: string, probe: string, max = 5): number[] {
  const out: number[] = [];
  let from = 0;
  while (out.length < max) {
    const at = looseIndexOf(haystack, probe, from);
    if (at < 0) break;
    out.push(at);
    from = at + 1;
  }
  return out;
}

/**
 * 在 `haystack` 里定位引用片段，返回 [start, end)；找不到返回 null。
 *
 * 起点用前缀探针（由长到短；短探针可能撞上重复文本，所以多取几个落点、挑贴得最长的
 * 那个）；终点靠 `extendMatch` 量出来，贴不动时再用尾部探针兜一次——务求把**整段**
 * 引用圈出来，而不是只圈探针那一小截。
 */
export function findCitedRange(haystack: string, fragment: string): [number, number] | null {
  const needle = normalizeCitedFragment(fragment);
  if (!haystack || needle.length < 6) return null;

  const probeLens = [200, 120, 60, 30, 14, 8];
  for (const len of probeLens) {
    const probe = needle.slice(0, Math.min(len, needle.length));
    if (probe.length < 6) break;

    let best: { start: number; end: number; consumed: number } | null = null;
    for (const start of candidateStarts(haystack, probe)) {
      const { end, consumed } = extendMatch(haystack, start, needle);
      if (!best || consumed > best.consumed) best = { start, end, consumed };
      if (consumed >= needle.length) break; // 整段贴上了，不用再挑
    }
    if (!best) {
      if (probe.length >= needle.length) break;
      continue;
    }

    const { start, consumed } = best;
    let end = best.end;
    // 中途岔开（正文里插了图注 / 片段被后端二次加工）→ 用尾部探针把终点补到末尾。
    // 只认"离起点不太远"的落点，免得跳到全文另一处同样的话上去。
    if (consumed < needle.length * 0.85) {
      const tail = needle.slice(-Math.min(40, needle.length));
      if (tail.length >= 8) {
        const at = looseIndexOf(haystack, tail, end);
        if (at >= 0 && at - start < needle.length * 3) {
          const tailEnd = extendMatch(haystack, at, tail).end;
          if (tailEnd > end) end = tailEnd;
        }
      }
    }
    return [start, Math.max(end, start + 1)];
  }
  return null;
}

/**
 * 命中是否"不值得高亮"。
 *
 * 两种退化情形，涂色只会误导：
 *  1. **从正文最开头开始**——整体型锚点（citation_anchor 的 L4 / 文本兜底）的 snippet
 *     就是结果的前 500 / 3000 字，命中必然落在 index 0。涂出来的是"正文开头那几百字"，
 *     跟模型到底引了哪句毫无关系。
 *  2. **几乎盖满全文**——列表型条目打开时正文本身就是那条记录，snippet 约等于全文，
 *     整篇涂色等于没涂。
 * 这两种一律不画高亮，卡片正常从头显示。定位不受影响：DataView 要的是"命中在哪条
 * 记录里"，继续用 findCitedRange。
 */
export function isTrivialCitedHit(haystack: string, range: [number, number]): boolean {
  if (haystack.slice(0, range[0]).trim().length < 10) return true;
  const covered = range[1] - range[0];
  return covered >= haystack.trim().length * 0.9;
}

/**
 * 渲染用的命中区间：定位到了、且值得高亮时才返回；退化命中返回 null。
 * 定位类调用（DataView 找命中记录）仍用 findCitedRange。
 */
export function findCitedHighlight(haystack: string, fragment?: string): [number, number] | null {
  if (!fragment) return null;
  const range = findCitedRange(haystack, fragment);
  if (!range) return null;
  return isTrivialCitedHit(haystack, range) ? null : range;
}
