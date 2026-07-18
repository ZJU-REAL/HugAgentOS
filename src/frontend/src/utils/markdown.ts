import { marked } from 'marked';
import hljs from 'highlight.js';

// Configure marked
marked.setOptions({
  breaks: true,
  gfm: true,
});

// Open all markdown links in a new tab
marked.use({
  renderer: {
    link({ href, title, text }: { href: string; title?: string | null; text: string }) {
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

// highlight.js integration for marked v17 (fallback for non-mermaid blocks)
marked.use({
  hooks: {
    postprocess(html: string) {
      return html;
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
