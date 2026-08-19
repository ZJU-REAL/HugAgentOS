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
// 换行、连续空白、尾部省略都会让精确匹配落空。这里逐级降级：
// 归一化 → 取前缀 → 空白宽松正则，前缀由长到短直到命中。

const ELLIPSIS_TAIL = /(?:…|\.{3,})+\s*$/;

/** 去掉尾部省略号并把连续空白压成单空格，得到可用于检索的"针"。 */
export function normalizeCitedFragment(raw: string): string {
  if (!raw) return '';
  return raw.replace(ELLIPSIS_TAIL, '').replace(/\s+/g, ' ').trim();
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * 在 `haystack` 里定位引用片段，返回 [start, end)；找不到返回 null。
 * 宁可只命中开头一小段，也好过整段落空——所以前缀会一路缩短再试。
 */
export function findCitedRange(haystack: string, fragment: string): [number, number] | null {
  const needle = normalizeCitedFragment(fragment);
  if (!haystack || needle.length < 6) return null;

  const probeLens = [200, 120, 60, 30, 14, 8];
  for (const len of probeLens) {
    const probe = needle.slice(0, Math.min(len, needle.length));
    if (probe.length < 6) break;

    const direct = haystack.indexOf(probe);
    if (direct >= 0) return [direct, direct + probe.length];

    // 空白宽松：片段里的空格可能对应原文里的换行 / 缩进
    try {
      const re = new RegExp(probe.trim().split(/\s+/).map(escapeRe).join('\\s+'));
      const m = re.exec(haystack);
      if (m && m[0]) return [m.index, m.index + m[0].length];
    } catch { /* 正则构造失败（片段过长）→ 继续缩短前缀 */ }

    if (probe.length >= needle.length) break;
  }
  return null;
}
