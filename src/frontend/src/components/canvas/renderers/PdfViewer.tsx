import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { TextContent } from 'pdfjs-dist/types/src/display/api';
import { t } from '../../../i18n';
import { useCanvasZoomTarget } from '../canvasZoom';
import {
  scrollIntoPane, useRegisterCanvasDocument,
  type CanvasDocument, type FindHit, type OutlineNode,
} from '../canvasDocument';
import { findOffsets, findTextRanges, MAX_FIND_HITS } from '../find/textSearch';
import { openPdfDocument, type PdfDocument, type PdfJsModule } from './pdfjs';
import { previewErrorMessage } from './previewError';
import './PdfViewer.css';

const CSS_UNITS = 96 / 72;
const PAGE_GAP = 12;
const PAD = 16;
// Keep one page's backing store well below browser canvas limits.
const MAX_CANVAS_PIXELS = 16_777_216;

interface PageSize { width: number; height: number }

/** Shared per-document state that pages and the find/outline providers read. */
class PdfSession {
  private texts = new Map<number, Promise<TextContent>>();
  private strings = new Map<number, Promise<string>>();
  private layers = new Map<number, HTMLElement>();
  private rangeCache = new Map<string, Range[]>();
  private listeners = new Set<() => void>();
  private waiters = new Map<number, Array<() => void>>();
  readonly lib: PdfJsModule;
  readonly doc: PdfDocument;
  readonly scrollToPage: (index: number, offsetY?: number) => void;
  /** Viewport y of `offsetY` (PDF units) on page `index`, or null while it is not laid out. */
  readonly locate: (index: number, offsetY: number) => number | null;

  constructor(
    lib: PdfJsModule, doc: PdfDocument,
    scrollToPage: (index: number, offsetY?: number) => void,
    locate: (index: number, offsetY: number) => number | null,
  ) {
    this.lib = lib;
    this.doc = doc;
    this.scrollToPage = scrollToPage;
    this.locate = locate;
  }

  textContent(index: number): Promise<TextContent> {
    let entry = this.texts.get(index);
    if (!entry) {
      entry = this.doc.getPage(index + 1).then((page) => page.getTextContent());
      // A failed page must not poison every later search or text layer.
      entry.catch(() => this.texts.delete(index));
      this.texts.set(index, entry);
    }
    return entry;
  }

  /** Joined page text for find; an unreadable page searches as empty. */
  pageText(index: number): Promise<string> {
    let entry = this.strings.get(index);
    if (!entry) {
      entry = this.textContent(index)
        .then((content) => content.items.map((item) => ('str' in item ? item.str : '')).join(''))
        .catch(() => { this.strings.delete(index); return ''; });
      this.strings.set(index, entry);
    }
    return entry;
  }

  setLayer(index: number, layer: HTMLElement | null) {
    if (layer) this.layers.set(index, layer); else this.layers.delete(index);
    this.rangeCache.clear();
    if (layer) {
      this.waiters.get(index)?.forEach((resolve) => resolve());
      this.waiters.delete(index);
    }
    this.listeners.forEach((listener) => listener());
  }

  private layerRanges(index: number, query: string): Range[] {
    const layer = this.layers.get(index);
    if (!layer) return [];
    const key = `${index}\u0000${query}`;
    let ranges = this.rangeCache.get(key);
    if (!ranges) {
      ranges = findTextRanges(layer, query);
      this.rangeCache.set(key, ranges);
    }
    return ranges;
  }

  private waitForLayer(index: number): Promise<void> {
    if (this.layers.has(index)) return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => resolve();
      this.waiters.set(index, [...(this.waiters.get(index) ?? []), done]);
      window.setTimeout(done, 3000);
    });
  }

  readonly find = {
    search: async (query: string): Promise<FindHit[]> => {
      const pages = Array.from({ length: this.doc.numPages }, (_, index) => index);
      const texts = await Promise.all(pages.map((index) => this.pageText(index)));
      const hits: FindHit[] = [];
      for (const index of pages) {
        const count = findOffsets(texts[index], query, MAX_FIND_HITS - hits.length).length;
        for (let k = 0; k < count; k += 1) {
          hits.push({
            range: () => this.layerRanges(index, query)[k] ?? null,
            reveal: async () => {
              if (!this.layers.has(index)) this.scrollToPage(index);
              await this.waitForLayer(index);
              const range = this.layerRanges(index, query)[k];
              if (range) scrollIntoPane(range);
            },
          });
        }
        if (hits.length >= MAX_FIND_HITS) break;
      }
      return hits;
    },
    subscribe: (listener: () => void) => {
      this.listeners.add(listener);
      return () => { this.listeners.delete(listener); };
    },
  };

  async outline(): Promise<OutlineNode[]> {
    const items = await this.doc.getOutline();
    let seq = 0;
    const resolve = async (dest: string | unknown[] | null): Promise<{ page: number; left: number; top: number | null } | null> => {
      const explicit = typeof dest === 'string' ? await this.doc.getDestination(dest) : dest;
      if (!Array.isArray(explicit) || explicit.length === 0) return null;
      const [ref, mode, ...args] = explicit as [unknown, { name?: string } | undefined, ...unknown[]];
      const page = typeof ref === 'number'
        ? ref
        : await this.doc.getPageIndex(ref as Parameters<PdfDocument['getPageIndex']>[0]).catch(() => null);
      if (page === null) return null;
      const num = (value: unknown) => (typeof value === 'number' ? value : null);
      if (mode?.name === 'XYZ') return { page, left: num(args[0]) ?? 0, top: num(args[1]) };
      if (mode?.name === 'FitH' || mode?.name === 'FitBH') return { page, left: 0, top: num(args[0]) };
      return { page, left: 0, top: null };
    };
    // Destinations are resolved up front so each entry knows where it sits and
    // the outline can follow the reading position.
    const place = async (dest: string | unknown[] | null): Promise<{ page: number; y: number } | null> => {
      const target = await resolve(dest).catch(() => null);
      if (!target) return null;
      if (target.top === null) return { page: target.page, y: 0 };
      const page = await this.doc.getPage(target.page + 1);
      const [, y] = page.getViewport({ scale: 1 }).convertToViewportPoint(target.left, target.top);
      return { page: target.page, y };
    };
    const build = (nodes: typeof items): Promise<OutlineNode[]> => Promise.all((nodes ?? []).map(async (item) => {
      const id = `pdf-${seq++}`;
      const [target, children] = await Promise.all([place(item.dest), build(item.items)]);
      return {
        id,
        title: item.title,
        children,
        open: () => { if (target) this.scrollToPage(target.page, target.y); },
        ...(target ? { top: () => this.locate(target.page, target.y) } : {}),
      };
    }));
    return build(items);
  }
}

function PdfPageView({ session, index, size, scale, active }: {
  session: PdfSession; index: number; size: PageSize; scale: number; active: boolean;
}) {
  const canvasHost = useRef<HTMLDivElement>(null);
  const layerRef = useRef<HTMLDivElement>(null);

  // The text layer is scale-independent (positioned via --total-scale-factor),
  // so it is built once per activation; only the bitmap re-renders on zoom.
  useEffect(() => {
    const layer = layerRef.current;
    if (!active || !layer) return;
    let cancelled = false;
    let textLayer: InstanceType<PdfJsModule['TextLayer']> | null = null;
    void (async () => {
      const [page, content] = await Promise.all([session.doc.getPage(index + 1), session.textContent(index)]);
      if (cancelled) return;
      textLayer = new session.lib.TextLayer({ textContentSource: content, container: layer, viewport: page.getViewport({ scale: 1 }) });
      await textLayer.render();
      if (!cancelled) session.setLayer(index, layer);
    })().catch(() => { /* text stays unselectable; the bitmap still renders */ });
    return () => {
      cancelled = true;
      textLayer?.cancel();
      layer.replaceChildren();
      session.setLayer(index, null);
    };
  }, [active, index, session]);

  useEffect(() => {
    const host = canvasHost.current;
    if (!active || !host) return;
    let task: ReturnType<Awaited<ReturnType<PdfDocument['getPage']>>['render']> | null = null;
    let cancelled = false;
    void (async () => {
      const page = await session.doc.getPage(index + 1);
      if (cancelled) return;
      const viewport = page.getViewport({ scale });
      const ratio = Math.min(window.devicePixelRatio || 1, Math.sqrt(MAX_CANVAS_PIXELS / (viewport.width * viewport.height)));
      const canvas = document.createElement('canvas');
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      task = page.render({ canvas, viewport, transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0] });
      await task.promise;
      // Swap only after the new bitmap is ready so zooming never flashes blank.
      if (!cancelled) host.replaceChildren(canvas);
    })().catch(() => { /* cancelled or broken page: placeholder stays */ });
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [active, index, scale, session]);

  useEffect(() => {
    if (active) return;
    canvasHost.current?.replaceChildren();
    // Drop the page's operator lists and decoded images once it leaves view.
    void session.doc.getPage(index + 1).then((page) => page.cleanup()).catch(() => {});
  }, [active, index, session]);

  return (
    <div
      className="jx-pdfPage"
      data-page={index + 1}
      style={{
        width: Math.floor(size.width * scale),
        height: Math.floor(size.height * scale),
        ['--total-scale-factor' as string]: scale,
      }}
    >
      <div ref={canvasHost} className="jx-pdfPage-canvas" />
      <div ref={layerRef} className="textLayer" />
    </div>
  );
}

export function PdfViewer({ data, onError }: { data: Uint8Array; onError: (message: string) => void }) {
  const { tabId, zoom, reportFitScale } = useCanvasZoomTarget();
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [session, setSession] = useState<PdfSession | null>(null);
  const [sizes, setSizes] = useState<PageSize[]>([]);
  const [avail, setAvail] = useState(0);
  const [visible, setVisible] = useState<ReadonlySet<number>>(new Set());
  const [current, setCurrent] = useState(1);
  const anchor = useRef({ page: 0, ratio: 0 });
  const scrollToPage = useRef<(index: number, offsetY?: number) => void>(() => {});
  const locate = useRef<(index: number, offsetY: number) => number | null>(() => null);

  useEffect(() => {
    let cancelled = false;
    let destroy: (() => void) | undefined;
    openPdfDocument(data).then(async (opened) => {
      destroy = opened.destroy;
      if (cancelled) { destroy(); return; }
      const first = (await opened.doc.getPage(1)).getViewport({ scale: 1 });
      if (cancelled) return;
      // Pages share the first page's size until each one is actually loaded.
      setSizes(Array.from({ length: opened.doc.numPages }, () => ({ width: first.width, height: first.height })));
      setSession(new PdfSession(opened.lib, opened.doc,
        (index, offsetY) => scrollToPage.current(index, offsetY),
        (index, offsetY) => locate.current(index, offsetY)));
    }).catch((error: unknown) => {
      if (!cancelled) onError(previewErrorMessage(error, t('PDF 预览失败')));
    });
    return () => {
      cancelled = true;
      destroy?.();
    };
  }, [data, onError]);

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    const observer = new ResizeObserver(() => setAvail(scroller.clientWidth - PAD * 2));
    observer.observe(scroller);
    return () => observer.disconnect();
  }, []);

  const ready = avail > 0;
  // The header shows 'fit' as a real percentage; 100% is CSS_UNITS per PDF point.
  const firstWidth = sizes[0]?.width;
  useEffect(() => {
    if (ready && firstWidth) reportFitScale(Math.max(0.1, avail / firstWidth) / CSS_UNITS);
  }, [avail, firstWidth, ready, reportFitScale]);
  const scaleOf = useCallback((size: PageSize) => (
    zoom === 'fit' ? Math.max(0.1, avail / size.width) : zoom * CSS_UNITS
  ), [avail, zoom]);

  // Lazily learn real page sizes for pages that come into view.
  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    visible.forEach((index) => {
      void session.doc.getPage(index + 1).then((page) => {
        const { width, height } = page.getViewport({ scale: 1 });
        if (cancelled) return;
        setSizes((prev) => (prev[index] && prev[index].width === width && prev[index].height === height
          ? prev
          : prev.map((size, i) => (i === index ? { width, height } : size))));
      });
    });
    return () => { cancelled = true; };
  }, [session, visible]);

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller || !session) return;
    const observer = new IntersectionObserver((entries) => {
      setVisible((prev) => {
        const next = new Set(prev);
        entries.forEach((entry) => {
          const index = Number((entry.target as HTMLElement).dataset.page) - 1;
          if (entry.isIntersecting) next.add(index); else next.delete(index);
        });
        return next;
      });
    }, { root: scroller, rootMargin: '150% 0px' });
    scroller.querySelectorAll('.jx-pdfPage').forEach((page) => observer.observe(page));
    return () => observer.disconnect();
  }, [session, sizes.length, ready]);

  const pageAt = useCallback((y: number) => {
    const pages = scrollerRef.current?.querySelectorAll<HTMLElement>('.jx-pdfPage');
    if (!pages?.length) return { index: 0, ratio: 0 };
    let lo = 0;
    let hi = pages.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (pages[mid].offsetTop <= y) lo = mid; else hi = mid - 1;
    }
    const page = pages[lo];
    return { index: lo, ratio: Math.max(0, (y - page.offsetTop) / page.offsetHeight) };
  }, []);

  const onScroll = useCallback(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    const top = pageAt(scroller.scrollTop + PAD);
    anchor.current = { page: top.index, ratio: top.ratio };
    setCurrent(pageAt(scroller.scrollTop + scroller.clientHeight / 3).index + 1);
  }, [pageAt]);

  // Keep the reading position when zoom or pane width rescales the pages.
  const scaleKey = sizes.map(scaleOf).join(',');
  useLayoutEffect(() => {
    const scroller = scrollerRef.current;
    const page = scroller?.querySelectorAll<HTMLElement>('.jx-pdfPage')[anchor.current.page];
    if (!scroller || !page) return;
    scroller.scrollTop = page.offsetTop - PAD + anchor.current.ratio * page.offsetHeight;
  }, [scaleKey]);

  useEffect(() => {
    scrollToPage.current = (index, offsetY = 0) => {
      const scroller = scrollerRef.current;
      const page = scroller?.querySelectorAll<HTMLElement>('.jx-pdfPage')[index];
      if (!scroller || !page) return;
      const size = sizes[index];
      scroller.scrollTop = page.offsetTop - PAD + (size ? offsetY * scaleOf(size) : 0);
    };
    locate.current = (index, offsetY) => {
      const page = scrollerRef.current?.querySelectorAll<HTMLElement>('.jx-pdfPage')[index];
      const size = sizes[index];
      return page && size ? page.getBoundingClientRect().top + offsetY * scaleOf(size) : null;
    };
  }, [scaleOf, sizes]);

  const canvasDoc = useMemo<CanvasDocument | null>(() => (session
    ? { find: session.find, outline: () => session.outline() }
    : null), [session]);
  useRegisterCanvasDocument(tabId, canvasDoc);

  return (
    <div className="jx-pdfViewer">
      <div ref={scrollerRef} className="jx-pdfViewer-scroll" data-canvas-scroller onScroll={onScroll}>
        {!session && <div className="jx-canvas-loading" aria-busy="true"><div className="jx-canvas-spinner" /><span>{t('正在加载 PDF…')}</span></div>}
        {session && ready && (
          <div className="jx-pdfViewer-pages jx-canvas-fadeIn" style={{ gap: PAGE_GAP, padding: PAD }}>
            {sizes.map((size, index) => (
              <PdfPageView key={index} session={session} index={index} size={size} scale={scaleOf(size)} active={visible.has(index)} />
            ))}
          </div>
        )}
      </div>
      {session && sizes.length > 0 && (
        <div className="jx-pdfViewer-pageIndicator" aria-label={t('第 {page} 页，共 {total} 页', { page: current, total: sizes.length })}>
          {current} / {sizes.length}
        </div>
      )}
    </div>
  );
}
