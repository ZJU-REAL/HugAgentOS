import highlighter, { requestLanguage } from './syntaxHighlighting';

/** Share the common/full grammar registry with React code views and exports. */
export async function ensureHighlighter(): Promise<void> { await import('highlight.js'); }

export function highlightSyntax(code: string, language?: string): string {
  if (language) requestLanguage(language);
  if (language && highlighter.getLanguage(language)) {
    try { return highlighter.highlight(code, { language }).value; } catch { /* escaped fallback */ }
  }
  return code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Markdown is shared by many HTML consumers. A lifecycle-bound element upgrades
// only its own immutable HTML island, without a global DOM observer or React state.
if (typeof customElements !== 'undefined' && !customElements.get('hugagent-code')) {
  customElements.define('hugagent-code', class extends HTMLElement {
    connectedCallback() {
      queueMicrotask(() => {
        const code = this.querySelector('pre > code');
        const language = code?.className.match(/(?:^|\s)language-(\S+)/)?.[1];
        if (!code || !language) return;
        const text = code.textContent ?? '';
        void ensureHighlighter().then(() => {
          if (this.isConnected && this.contains(code) && code.textContent === text) {
            code.innerHTML = highlightSyntax(text, language);
          }
        }).catch(() => { /* Keep readable escaped text on a failed chunk load. */ });
      });
    }
  });
}

export function lazyHighlightedHtml(html: string, language?: string): string {
  return !language || highlighter.getLanguage(language) ? html : `<hugagent-code style="display:contents">${html}</hugagent-code>`;
}
