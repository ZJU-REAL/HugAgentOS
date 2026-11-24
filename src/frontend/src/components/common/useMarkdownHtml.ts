import { useMemo } from 'react';

import { mdToHtml } from '../../utils/markdown';

/**
 * Sanitized HTML for a markdown string, memoized on the text.
 * `mdToHtml` runs marked + DOMPurify, which is too costly to repeat on every render
 * of a large panel; empty input yields an empty string.
 */
export function useMarkdownHtml(markdown: string | null | undefined): string {
  return useMemo(() => (markdown ? mdToHtml(markdown) : ''), [markdown]);
}
