import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { CloseOutlined, DownOutlined, SearchOutlined, UpOutlined } from '@ant-design/icons';
import { t } from '../../../i18n';
import { useCanvasPane, useCanvasPaneId } from '../useCanvasPane';
import { paneElements, useCanvasDocument, usePaneSurface, type FindHit } from '../canvasDocument';
import { domFindSource } from './domFindSource';
import './CanvasFind.css';

const highlightsSupported = () => typeof Highlight !== 'undefined' && typeof CSS !== 'undefined' && 'highlights' in CSS;

/**
 * Ctrl/⌘+F belongs to the pane holding focus; the hovered pane only claims it
 * while nothing else is focused, so the chat input keeps native find and only
 * one of two split panes ever answers.
 */
function ownsShortcut(anchor: Element | null) {
  const { pane } = paneElements(anchor);
  if (!pane) return false;
  const active = document.activeElement;
  if (pane.contains(active)) return true;
  return (!active || active === document.body) && pane.matches(':hover');
}

/**
 * Header button + floating find bar for the pane. Matches are painted with the
 * CSS Custom Highlight API so renderer DOM is never mutated.
 */
export function CanvasFind() {
  const paneId = useCanvasPaneId();
  const tabId = useCanvasPane((s) => s.activeTabId);
  const openSeq = useCanvasPane((s) => s.openSeq);
  const doc = useCanvasDocument(tabId);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<FindHit[]>([]);
  const [current, setCurrent] = useState(0);
  const [searching, setSearching] = useState(false);
  const [paintTick, setPaintTick] = useState(0);
  const surface = usePaneSurface(buttonRef, open, `${tabId}:${openSeq}`);
  const body = surface?.body ?? null;
  const source = useMemo(() => doc?.find ?? (body ? domFindSource(body) : null), [doc, body]);
  const lastQuery = useRef('');

  // Each open / Ctrl+F asks for focus; served once the bar has mounted in the pane.
  const [focusRequest, setFocusRequest] = useState(0);
  const focusInput = useCallback(() => setFocusRequest((n) => n + 1), []);
  const mounted = open && !!surface;
  useEffect(() => {
    if (!mounted) return;
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [focusRequest, mounted]);

  const close = useCallback(() => {
    setOpen(false);
    buttonRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey || event.key.toLowerCase() !== 'f') return;
      if (!ownsShortcut(buttonRef.current)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      setOpen(true);
      focusInput();
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [focusInput]);

  // Search, and re-search when the rendered content changes (late docx render,
  // pdf pages entering view). Content refreshes keep the current position.
  useEffect(() => {
    if (!open || !source) return;
    let cancelled = false;
    let timer = 0;
    const run = (revealFirst: boolean) => {
      if (!query) { setHits([]); setCurrent(0); return; }
      setSearching(true);
      void source.search(query).then((found) => {
        if (cancelled) return;
        setSearching(false);
        setHits(found);
        setPaintTick((tick) => tick + 1);
        if (revealFirst) {
          setCurrent(0);
          void found[0]?.reveal();
        } else {
          setCurrent((index) => Math.min(index, Math.max(0, found.length - 1)));
        }
      }).catch(() => { if (!cancelled) setSearching(false); });
    };
    run(lastQuery.current !== query);
    lastQuery.current = query;
    const unsubscribe = source.subscribe(() => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => run(false), 150);
    });
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      unsubscribe();
    };
  }, [open, query, source]);

  useEffect(() => {
    if (!open) lastQuery.current = '';
  }, [open]);

  // Paint all hits and the current one; names are per pane so split panes don't clobber each other.
  useEffect(() => {
    if (!open || hits.length === 0) return;
    const ranges = hits.map((hit) => hit.range());
    const currentRange = ranges[current];
    if (!highlightsSupported()) {
      if (currentRange) {
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(currentRange);
      }
      return;
    }
    const allName = `jx-canvas-find-${paneId}`;
    const currentName = `jx-canvas-find-${paneId}-current`;
    CSS.highlights.set(allName, new Highlight(...ranges.filter((range): range is Range => !!range)));
    if (currentRange) CSS.highlights.set(currentName, new Highlight(currentRange));
    return () => {
      CSS.highlights.delete(allName);
      CSS.highlights.delete(currentName);
    };
  }, [current, hits, open, paneId, paintTick]);

  const step = (delta: number) => {
    if (hits.length === 0) return;
    const next = (current + delta + hits.length) % hits.length;
    setCurrent(next);
    void hits[next].reveal().then(() => setPaintTick((tick) => tick + 1));
  };

  const status = !query
    ? ''
    : searching && hits.length === 0
      ? t('正在查找…')
      : hits.length === 0
        ? t('无结果')
        : `${current + 1}/${hits.length}`;

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className="jx-canvas-actionBtn"
        aria-label={t('在文件中查找')}
        title={t('在文件中查找')}
        aria-pressed={open}
        onClick={() => {
          if (open) { close(); return; }
          setOpen(true);
          focusInput();
        }}
      >
        <SearchOutlined />
      </button>
      {open && surface && createPortal(
        <div className="jx-canvasFind" role="search" style={{ top: surface.top + 8 }} data-find-skip>
          <input
            ref={inputRef}
            className="jx-canvasFind-input"
            value={query}
            placeholder={t('查找')}
            aria-label={t('查找内容')}
            spellCheck={false}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') { event.preventDefault(); step(event.shiftKey ? -1 : 1); }
              if (event.key === 'Escape') { event.preventDefault(); close(); }
            }}
          />
          <span className="jx-canvasFind-count" role="status" aria-live="polite">{status}</span>
          <button type="button" className="jx-canvas-actionBtn" aria-label={t('上一个')} title={t('上一个 (Shift+Enter)')}
            disabled={hits.length === 0} onClick={() => step(-1)}><UpOutlined /></button>
          <button type="button" className="jx-canvas-actionBtn" aria-label={t('下一个')} title={t('下一个 (Enter)')}
            disabled={hits.length === 0} onClick={() => step(1)}><DownOutlined /></button>
          <button type="button" className="jx-canvas-actionBtn" aria-label={t('关闭查找')} title={t('关闭查找 (Esc)')}
            onClick={close}><CloseOutlined /></button>
        </div>,
        surface.pane,
      )}
    </>
  );
}
