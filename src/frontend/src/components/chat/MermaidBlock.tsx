import { useEffect, useRef, useState } from 'react';
import { t } from '../../i18n';

let mermaidModule: typeof import('mermaid') | null = null;
let mermaidLoading: Promise<typeof import('mermaid')> | null = null;
let mermaidInitialized = false;

async function getMermaid() {
  if (mermaidModule) return mermaidModule;
  if (!mermaidLoading) {
    mermaidLoading = import('mermaid').then((m) => {
      mermaidModule = m;
      if (!mermaidInitialized) {
        m.default.initialize({
          startOnLoad: false,
          theme: 'default',
          securityLevel: 'loose',
          fontFamily: '"PingFang SC", "Microsoft YaHei", sans-serif',
        });
        mermaidInitialized = true;
      }
      return m;
    });
  }
  return mermaidLoading;
}

let idCounter = 0;

export function MermaidBlock({ chart }: { chart: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [rendered, setRendered] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const render = async () => {
      try {
        const mermaid = await getMermaid();
        if (cancelled || !containerRef.current) return;
        const id = `jx-mermaid-${++idCounter}`;
        const { svg } = await mermaid.default.render(id, chart);
        if (cancelled || !containerRef.current) return;
        containerRef.current.innerHTML = svg;
        setRendered(true);
      } catch (e: any) {
        if (!cancelled) setError(e?.message || t('Mermaid 渲染失败'));
      }
    };
    render();
    return () => { cancelled = true; };
  }, [chart]);

  if (error) {
    return (
      <div className="jx-mermaid jx-mermaid--error">
        <pre><code>{chart}</code></pre>
        <div className="jx-mermaid-errorMsg">{error}</div>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className={`jx-mermaid${rendered ? ' jx-mermaid--rendered' : ''}`}
    >
      {!rendered && <div className="jx-mermaid-loading">{t('加载图表中...')}</div>}
    </div>
  );
}

/**
 * Scan a container for .jx-mermaid[data-chart] elements and return
 * decoded chart sources. Used by CitationMarkdownBlock to find mermaid
 * placeholders inserted by markdown.ts renderer.
 */
export function extractMermaidCharts(container: HTMLElement): Array<{ element: HTMLElement; chart: string }> {
  const results: Array<{ element: HTMLElement; chart: string }> = [];
  const elements = container.querySelectorAll<HTMLElement>('.jx-mermaid[data-chart]');
  elements.forEach((el) => {
    const encoded = el.getAttribute('data-chart');
    if (!encoded) return;
    try {
      const chart = decodeURIComponent(atob(encoded));
      results.push({ element: el, chart });
    } catch {
      // invalid encoding, skip
    }
  });
  return results;
}
