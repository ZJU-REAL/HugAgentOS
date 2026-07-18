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
