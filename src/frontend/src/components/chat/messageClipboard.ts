import { getSchema } from '@tiptap/core';
import { MarkdownManager } from '@tiptap/markdown';
import { Marked } from 'marked';
import DOMPurify from 'dompurify';
import type { ChatMessage } from '../../types';
import { markdownAutolinks } from '../../utils/markdownAutolinks';
import { markdownCodeHtml, markdownListHtml, markdownParagraphHtml, userMarkdownHtml, plainTextHtml } from '../../utils/userMarkdown';
import { editorMarkdownParser } from './composerMarkdown';
import { composerExtensions } from './composerExtensions';
import { parseClipboardHtml } from './composerClipboard';

// Assistant HTML has presentation semantics. User HTML remains literal source.
const assistantMarkdown = new Marked({ gfm: true, breaks: true, tokenizer: markdownAutolinks, renderer: { code: markdownCodeHtml, list: markdownListHtml, paragraph: markdownParagraphHtml } });
const extensions = composerExtensions();
const schema = getSchema(extensions);
const markdown = new MarkdownManager({ extensions, marked: editorMarkdownParser() });

/** Empty stream placeholders are not paragraphs; authored boundary breaks stay intact. */
function messageText(message: ChatMessage): string {
  const parts = message.segments?.filter(segment => segment.type === 'text' && segment.content?.trim())
    .map(segment => segment.content!) ?? [];
  if (!parts.length) return message.content;
  const chunks: string[] = [];
  let previousTail = 0;
  for (const part of parts) {
    let leading = 0;
    while (part[leading] === '\n') leading += 1;
    if (chunks.length) chunks.push('\n'.repeat(Math.max(0, 2 - previousTail - leading)));
    chunks.push(part);
    previousTail = 0;
    while (part[part.length - previousTail - 1] === '\n') previousTail += 1;
  }
  return chunks.join('');
}

/** Both clipboard flavors describe the same message; HTML is never copied as source by accident. */
export function messageClipboard(message: ChatMessage): { html: string; plainText: string } {
  const text = messageText(message);
  if (message.role === 'user') return { html: userMarkdownHtml(text), plainText: text };
  if (!message.isMarkdown) return { html: plainTextHtml(text), plainText: text };
  const html = DOMPurify.sanitize(assistantMarkdown.parse(text) as string);
  const content = parseClipboardHtml(schema, html);
  return { html, plainText: markdown.serialize({ type: 'doc', content: content.toJSON() }) };
}
