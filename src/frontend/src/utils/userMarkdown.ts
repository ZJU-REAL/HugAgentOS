import { Marked } from 'marked';
import DOMPurify from 'dompurify';

const renderer = new Marked({
  gfm: true, breaks: true,
  renderer: {
    html({ text }) {
      return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    },
  },
});

/** User code, including Mermaid, stays visible; raw HTML is literal Markdown text. */
export function userMarkdownHtml(text: string): string {
  return DOMPurify.sanitize(renderer.parse(text) as string);
}
