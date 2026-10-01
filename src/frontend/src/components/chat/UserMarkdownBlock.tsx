import { useMemo } from 'react';
import { userMarkdownHtml } from '../../utils/userMarkdown';

/** User text is literal Markdown: assistant citation resolution must never remove it. */
export function UserMarkdownBlock({ text, className }: { text: string; className?: string }) {
  const html = useMemo(() => userMarkdownHtml(text), [text]);
  return <div className={className} dangerouslySetInnerHTML={{ __html: html }} />;
}
