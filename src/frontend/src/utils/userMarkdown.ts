import { Marked, Renderer, type Tokens } from 'marked';
import DOMPurify from 'dompurify';
import { markdownAutolinks } from './markdownAutolinks';

const renderer = new Marked({
  gfm: true, breaks: true, tokenizer: markdownAutolinks,
  renderer: {
    code: markdownCodeHtml,
    list: markdownListHtml,
    paragraph: markdownParagraphHtml,
    html({ text, block }) {
      return block ? markdownCodeHtml({ text }) : escapeHtml(text);
    },
  },
});

/** User code, including Mermaid, stays visible; raw HTML is literal Markdown text. */
export function userMarkdownHtml(text: string): string {
  return DOMPurify.sanitize(renderer.parse(text) as string);
}

function escapeHtml(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/** A plain message's line breaks are content, not HTML source whitespace. */
export function plainTextHtml(text: string): string {
  return '<p data-whitespace="preserve">' + escapeHtml(text).replace(/\r\n?|\n/g, '<br>') + '</p>';
}

/** Rendered Markdown text carries authored whitespace, unlike HTML layout source. */
export function markdownParagraphHtml(this: Renderer, { tokens }: Tokens.Paragraph): string {
  return '<p data-whitespace="preserve">' + this.parser.parseInline(tokens) + '</p>';
}

/** Code tokens already contain authored newlines; rendering must not append another. */
export function markdownCodeHtml({ text, lang }: Pick<Tokens.Code, 'text' | 'lang'>): string {
  const language = lang?.split(/\s/)[0];
  return '<pre><code' + (language ? ' class="language-' + escapeHtml(language) + '"' : '')
    + '>' + escapeHtml(text) + '</code></pre>';
}

/** Task state belongs to the list structure, rather than a disposable display checkbox. */
export function markdownListHtml(this: Renderer, token: Tokens.List): string {
  if (!token.items.some(item => item.task)) return Renderer.prototype.list.call(this, token);
  const tag = token.ordered ? 'ol' : 'ul';
  const attributes = token.ordered
    ? (token.start !== '' && token.start !== 1 ? ' start="' + Number(token.start) + '"' : '')
    : ' data-type="taskList"';
  const items = token.items.map(item => {
    const content = this.parser.parse(item.tokens.filter(token => token.type !== 'checkbox'));
    if (!item.task) return '<li>' + content + '</li>';
    const checked = item.checked ? 'true' : 'false';
    return '<li data-type="taskItem" data-checked="' + checked + '">'
      + '<label><input type="checkbox" disabled' + (item.checked ? ' checked' : '')
      + '></label><div>' + content + '</div></li>';
  }).join('');
  return '<' + tag + attributes + '>' + items + '</' + tag + '>';
}
