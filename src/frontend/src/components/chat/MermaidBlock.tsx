import { useEffect, useState } from 'react';
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

interface MermaidRenderState {
  chart: string;
  svg?: string;
  error?: string;
}

export function MermaidBlock({ chart }: { chart: string }) {
  const [renderState, setRenderState] = useState<MermaidRenderState | null>(null);
  const currentRender = renderState?.chart === chart ? renderState : null;

  useEffect(() => {
    let cancelled = false;
    const render = async () => {
      try {
        const mermaid = await getMermaid();
        if (cancelled) return;
        const id = `jx-mermaid-${++idCounter}`;
        const { svg } = await mermaid.default.render(id, chart);
        if (!cancelled) setRenderState({ chart, svg });
      } catch (error: unknown) {
        if (!cancelled) {
          setRenderState({
            chart,
            error: error instanceof Error ? error.message : t('Mermaid 渲染失败'),
          });
        }
      }
    };
    void render();
    return () => { cancelled = true; };
  }, [chart]);

  if (currentRender?.error) {
    return (
      <div className="jx-mermaid jx-mermaid--error">
        <pre><code>{chart}</code></pre>
        <div className="jx-mermaid-errorMsg">{currentRender.error}</div>
      </div>
    );
  }

  if (typeof currentRender?.svg === 'string') {
    // React must remain the sole owner of this subtree. Writing to a ref's
    // innerHTML here would remove the loading node behind React's back and
    // make the next reconciliation fail with a removeChild NotFoundError.
    return (
      <div
        className="jx-mermaid jx-mermaid--rendered"
        dangerouslySetInnerHTML={{ __html: currentRender.svg }}
      />
    );
  }

  return (
    <div className="jx-mermaid">
      <div className="jx-mermaid-loading">{t('加载图表中...')}</div>
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
