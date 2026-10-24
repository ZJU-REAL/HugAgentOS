import { useEffect, useMemo, useState } from 'react';
import { t } from '../../../i18n';
import { authFetch } from '../../../api';
import { CitationMarkdownBlock } from '../../citation';
import { readLimitedText } from '../../../utils/filePreviewSafety';
import { langFromPath } from '../../../utils/codeExecParser';
import { highlightSyntax } from '../../../utils/syntaxHighlight';
import { useSyntaxHighlighting } from '../../../utils/syntaxHighlighting';
import { loadJsonPref, saveJsonPref } from '../../../storage';
import { previewErrorMessage } from './previewError';
import './TextRenderers.css';

// Highlighting multi-megabyte logs costs seconds of main-thread time for little value.
const MAX_HIGHLIGHT_CHARS = 300_000;
const WRAP_PREF_KEY = 'jx_canvas_code_wrap';

function useRemoteText(url: string, maxBytes: number) {
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    (async () => {
      try {
        setLoading(true);
        setError(null);
        const resp = await authFetch(url, { signal: controller.signal });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const text = await readLimitedText(resp, maxBytes);
        if (!cancelled) setContent(text);
      } catch (error: unknown) {
        if (!cancelled) setError(previewErrorMessage(error, t('文本加载失败')));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [maxBytes, url]);

  return { content, loading, error };
}

/**
 * Split highlight.js HTML into per-line HTML, closing spans at each newline and
 * reopening them on the next line so every line is well-formed on its own.
 */
function splitHighlightedLines(html: string): string[] {
  const lines: string[] = [];
  const open: string[] = [];
  let line = '';
  for (const [, openTag, closeTag, newline, text] of html.matchAll(/(<span[^>]*>)|(<\/span>)|(\n)|([^<\n]+)/g)) {
    if (openTag) { open.push(openTag); line += openTag; }
    else if (closeTag) { open.pop(); line += closeTag; }
    else if (newline) { lines.push(line + '</span>'.repeat(open.length)); line = open.join(''); }
    else line += text;
  }
  lines.push(line + '</span>'.repeat(open.length));
  return lines;
}

function WrapGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M2 3.5h12M2 8h9.5a2.5 2.5 0 0 1 0 5H8m0 0 1.75-1.75M8 13l1.75 1.75M2 12.5h3.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function TextRenderer({ url, name, maxBytes }: { url: string; name: string; maxBytes: number }) {
  const { content, loading, error } = useRemoteText(url, maxBytes);
  const [wrap, setWrap] = useState(() => loadJsonPref(WRAP_PREF_KEY, { wrap: false }).wrap);
  const language = langFromPath(name);
  const grammarRevision = useSyntaxHighlighting();

  const { html, lineCount } = useMemo(() => {
    // grammarRevision bumps once the full grammar set has loaded: re-highlight then.
    const highlighted = grammarRevision >= 0 && language !== 'text' && content.length <= MAX_HIGHLIGHT_CHARS;
    const lines = splitHighlightedLines(highlightSyntax(content, highlighted ? language : undefined));
    return {
      html: lines.map((line) => `<div class="jx-codeView-line"><span>${line}</span></div>`).join(''),
      lineCount: lines.length,
    };
  }, [content, language, grammarRevision]);

  const toggleWrap = () => {
    setWrap((value) => {
      saveJsonPref(WRAP_PREF_KEY, { wrap: !value });
      return !value;
    });
  };

  if (error) return <div className="jx-canvas-error">{error}</div>;
  if (loading) return <div className="jx-canvas-loading" aria-busy="true"><div className="jx-canvas-spinner" /><span>{t('正在加载…')}</span></div>;
  return (
    <div className={`jx-codeView jx-canvas-fadeIn${wrap ? ' jx-codeView--wrap' : ''}`}>
      <div className="jx-codeView-toolbar" data-find-skip>
        <button
          type="button"
          className="jx-canvas-actionBtn"
          aria-pressed={wrap}
          aria-label={t('自动换行')}
          title={wrap ? t('关闭自动换行') : t('自动换行')}
          onClick={toggleWrap}
        >
          <WrapGlyph />
        </button>
        {language !== 'text' && <span className="jx-codeView-lang">{language}</span>}
      </div>
      <pre className="jx-codeView-pre">
        <code
          className="hljs jx-codeView-code"
          style={{ ['--jx-code-gutter' as string]: `${String(lineCount).length + 1}ch` }}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      </pre>
    </div>
  );
}

export function MarkdownRenderer({ url, maxBytes }: { url: string; maxBytes: number }) {
  const { content, loading, error } = useRemoteText(url, maxBytes);

  if (error) return <div className="jx-canvas-error">{error}</div>;
  if (loading) return <div className="jx-canvas-loading" aria-busy="true"><div className="jx-canvas-spinner" /><span>{t('正在加载…')}</span></div>;
  return (
    <CitationMarkdownBlock
      text={content}
      isMarkdown
      citations={[]}
      className="jx-canvas-markdown jx-md jx-canvas-fadeIn"
    />
  );
}
