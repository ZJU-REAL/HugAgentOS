import type { CSSProperties } from 'react';

import { useMarkdownHtml } from './useMarkdownHtml';

interface MarkdownHtmlProps {
  markdown: string | null | undefined;
  className?: string;
  style?: CSSProperties;
}

/** Renders sanitized markdown (see `useMarkdownHtml`); the conversion is memoized on the text. */
export function MarkdownHtml({ markdown, className, style }: MarkdownHtmlProps) {
  const html = useMarkdownHtml(markdown);
  return <div className={className} style={style} dangerouslySetInnerHTML={{ __html: html }} />;
}
