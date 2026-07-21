import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { ReloadOutlined, ZoomInOutlined, ZoomOutOutlined } from '@ant-design/icons';
import { t } from '../../i18n';

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 3;
const ZOOM_STEP = 0.25;

function clampZoom(value: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));
}

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
  const [zoom, setZoom] = useState(1);
  const currentRender = renderState?.chart === chart ? renderState : null;
  // Keep the innerHTML prop stable while zoom changes. Reassigning innerHTML
  // would recreate the SVG and replay its entrance animation on every click.
  const svgHtml = useMemo(
    () => typeof currentRender?.svg === 'string' ? { __html: currentRender.svg } : null,
    [currentRender?.svg],
  );

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

  if (svgHtml) {
    // React must remain the sole owner of this subtree. Writing to a ref's
    // innerHTML here would remove the loading node behind React's back and
    // make the next reconciliation fail with a removeChild NotFoundError.
    return (
      <div className="jx-mermaid jx-mermaid--rendered">
        <div className="jx-mermaid-toolbar">
          <button
            type="button"
            className="jx-mermaid-zoomButton"
            title={t('缩小')}
            aria-label={t('缩小')}
            disabled={zoom <= MIN_ZOOM}
            onClick={() => setZoom((value) => clampZoom(value - ZOOM_STEP))}
          >
            <ZoomOutOutlined />
          </button>
          <span className="jx-mermaid-zoomValue" aria-live="polite">
            {Math.round(zoom * 100)}%
          </span>
          <button
            type="button"
            className="jx-mermaid-zoomButton"
            title={t('放大')}
            aria-label={t('放大')}
            disabled={zoom >= MAX_ZOOM}
            onClick={() => setZoom((value) => clampZoom(value + ZOOM_STEP))}
          >
            <ZoomInOutlined />
          </button>
          <button
            type="button"
            className="jx-mermaid-zoomButton"
            title={t('重置')}
            aria-label={t('重置')}
            disabled={zoom === 1}
            onClick={() => setZoom(1)}
          >
            <ReloadOutlined />
          </button>
        </div>
        <div className="jx-mermaid-viewport">
          <div
            className="jx-mermaid-svgHost"
            style={{ '--jx-mermaid-width': `${zoom * 100}%` } as CSSProperties}
            dangerouslySetInnerHTML={svgHtml}
          />
        </div>
      </div>
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
