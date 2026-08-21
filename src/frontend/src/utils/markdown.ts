import { marked } from 'marked';
import hljs from 'highlight.js';
import { wrapTablesWithCopy } from './tableCopy';

// Configure marked
marked.setOptions({
  breaks: true,
  gfm: true,
});

// Open all markdown links in a new tab
marked.use({
  renderer: {
    link({ href, title, text }: { href: string; title?: string | null; text: string }) {
      // 证据锚点 [锚文本](cite:eN) 借用了 markdown 链接语法，但它**不是可跳转链接**
      // （cite: 不是有效协议，点进去是空白页）。对话区由 CitationMarkdownBlock 在
      // 进 marked 之前就换成占位符、交给 React 渲染带悬浮卡的标注；这里兜住所有
      // 直接调 mdToHtml 的路径（PDF 导出等），渲染成同款静态标注而非 <a>。
      // 容错与 utils/citations 的 citationMarkerRe 保持一致：`cite:` 后允许空白、
      // 锚点前缀 e 允许缺省或大写。收紧成 `^cite:(e\d+)$` 时，`cite: E8` / `cite:8`
      // 这些模型实际写出来的变体会掉进下面的 <a> 分支，变成一个点不开的死链接。
      const citeMatch = /^cite:\s*e?(\d+)\s*$/i.exec(href || '');
      if (citeMatch) {
        const num = citeMatch[1];
        return `<span class="jx-citeRef" data-cite="e${num}">${text}<sup class="jx-citeRef-idx">${num}</sup></span>`;
      }
      // 兜底：任何其它 cite: 开头的 href 也绝不能渲染成 <a> —— cite: 不是可跳转协议，
      // 点开是空白页。退化成纯文本，至少锚文本还在。
      if (/^cite:/i.test(href || '')) return text;
      const titleAttr = title ? ` title="${title}"` : '';
      return `<a href="${href}"${titleAttr} target="_blank" rel="noopener noreferrer">${text}</a>`;
    },
    // Mermaid code blocks → placeholder div (rendered by MermaidBlock component)
    code({ text, lang }: { text: string; lang?: string }) {
      if (lang === 'mermaid') {
        const encoded = btoa(encodeURIComponent(text));
        return `<div class="jx-mermaid" data-chart="${encoded}"></div>`;
      }
      // Default code block with highlight.js
      let highlighted = text;
      if (lang && hljs.getLanguage(lang)) {
        try {
          highlighted = hljs.highlight(text, { language: lang }).value;
        } catch {
          // fallback to raw text
        }
      }
      return `<pre><code class="hljs${lang ? ` language-${lang}` : ''}">${highlighted}</code></pre>`;
    },
  },
});

// ── LaTeX support via KaTeX (lazy-loaded) ──────────────────────────

let katexModule: typeof import('katex') | null = null;
let katexLoading: Promise<typeof import('katex')> | null = null;
let katexCssInjected = false;

function injectKatexCss() {
  if (katexCssInjected) return;
  katexCssInjected = true;
  // Dynamically import the CSS from the installed katex package (no CDN dependency)
  import('katex/dist/katex.min.css');
}

async function getKatex() {
  if (katexModule) return katexModule;
  if (!katexLoading) {
    katexLoading = import('katex').then((m) => {
      katexModule = m;
      return m;
    });
  }
  return katexLoading;
}

/** Synchronous KaTeX render — returns raw HTML or null if not yet loaded */
function renderKatexSync(expr: string, displayMode: boolean): string | null {
  if (!katexModule) return null;
  try {
    return katexModule.default.renderToString(expr, {
      displayMode,
      throwOnError: false,
      output: 'html',
    });
  } catch {
    return `<code class="jx-katex-error">${expr}</code>`;
  }
}

// ── LaTeX marked extensions ────────────────────────────────────────

// Block-level: $$...$$
const blockLatexExtension = {
  name: 'blockLatex',
  level: 'block',
  start(src: string) { return src.indexOf('$$'); },
  tokenizer(src: string) {
    const match = src.match(/^\$\$([\s\S]+?)\$\$/);
    if (match) {
      return { type: 'blockLatex', raw: match[0], text: match[1].trim() };
    }
    return undefined;
  },
  renderer(token: any) {
    const html = renderKatexSync(token.text, true);
    if (html) return `<div class="katex-display">${html}</div>`;
    // Fallback: show code until KaTeX loads
    return `<div class="katex-display"><code>${token.text}</code></div>`;
  },
};

// Inline-level: $...$
const inlineLatexExtension = {
  name: 'inlineLatex',
  level: 'inline',
  start(src: string) { return src.indexOf('$'); },
  tokenizer(src: string) {
    // Match $...$ but not $$...$$ and not escaped \$
    const match = src.match(/^\$([^\$\n]+?)\$/);
    if (match) {
      return { type: 'inlineLatex', raw: match[0], text: match[1].trim() };
    }
    return undefined;
  },
  renderer(token: any) {
    const html = renderKatexSync(token.text, false);
    if (html) return html;
    return `<code>${token.text}</code>`;
  },
};

marked.use({ extensions: [blockLatexExtension, inlineLatexExtension] });

// Postprocess: wrap every rendered <table> with a copy-button container
// (rich-HTML copy so pasting into Word keeps a real table — see utils/tableCopy.ts)
marked.use({
  hooks: {
    postprocess(html: string) {
      return wrapTablesWithCopy(html);
    },
  },
  async: false,
} as any);

export function mdToHtml(md: string): string {
  return marked.parse(md) as string;
}

/**
 * Ensure KaTeX is loaded. Call this after rendering markdown that might
 * contain LaTeX. Returns true if KaTeX was freshly loaded (re-render needed).
 */
export async function ensureKatexLoaded(): Promise<boolean> {
  if (katexModule) return false;
  injectKatexCss();
  await getKatex();
  return true;
}

/** Check if text contains LaTeX markers */
export function hasLatex(text: string): boolean {
  return /\$[^\$\n]+?\$/.test(text) || /\$\$[\s\S]+?\$\$/.test(text);
}

/** Check if text contains mermaid code blocks */
export function hasMermaid(text: string): boolean {
  return /```mermaid\b/.test(text);
}

export function parseFrontmatter(content: string): { frontmatter: Record<string, string>; body: string } {
  const frontmatterRegex = /^---\n([\s\S]*?)\n---\n([\s\S]*)$/;
  const match = content.match(frontmatterRegex);

  if (!match) {
    return { frontmatter: {}, body: content };
  }

  const frontmatterText = match[1];
  const body = match[2];
  const frontmatter: Record<string, string> = {};

  frontmatterText.split('\n').forEach(line => {
    const colonIndex = line.indexOf(':');
    if (colonIndex > 0) {
      const key = line.slice(0, colonIndex).trim();
      const value = line.slice(colonIndex + 1).trim();
      frontmatter[key] = value;
    }
  });

  return { frontmatter, body };
}
