import { useMemo } from 'react';
import { userMarkdownHtml } from '../../utils/userMarkdown';

/** User text is literal Markdown: assistant citation resolution must never remove it. */
export function UserMarkdownBlock({ text, className }: { text: string; className?: string }) {
  // The same object until the text changes: React rewrites innerHTML whenever it gets a new one, which on every
  // re-render would drop a text selection and, in WebKit, move a scrolled-up view.
  const html = useMemo(() => ({ __html: userMarkdownHtml(text) }), [text]);
  return <div className={className} dangerouslySetInnerHTML={html} />;
}
