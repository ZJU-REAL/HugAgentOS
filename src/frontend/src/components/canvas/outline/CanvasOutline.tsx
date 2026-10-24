import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { CloseOutlined, RightOutlined, UnorderedListOutlined } from '@ant-design/icons';
import { t } from '../../../i18n';
import { useCanvasPane } from '../useCanvasPane';
import { contentScroller, useCanvasDocument, usePaneSurface, type OutlineNode } from '../canvasDocument';
import { domOutline } from './domOutline';
import './CanvasOutline.css';

/** Where a heading counts as "being read": this far below the top of the scroll viewport. */
const READING_LINE = 48;
/** After a click, the jump's own scrolling must not move the selection off the picked entry. */
const PICK_SETTLE_MS = 800;

function contains(node: OutlineNode, id: string | null): boolean {
  return !!id && node.children.some((child) => child.id === id || contains(child, id));
}

function OutlineItem({ node, depth, activeId, onPick }: {
  node: OutlineNode; depth: number; activeId: string | null; onPick: (node: OutlineNode) => void;
}) {
  const [expanded, setExpanded] = useState(depth < 2);
  const hasChildren = node.children.length > 0;
  // A collapsed parent stands in for the section being read inside it.
  const active = node.id === activeId || (!expanded && contains(node, activeId));
  return (
    <li role="treeitem" aria-expanded={hasChildren ? expanded : undefined} aria-current={active ? 'location' : undefined}>
      <div className={`jx-canvasOutline-row${active ? ' is-active' : ''}${depth === 0 ? ' is-top' : ''}`}
        style={{ paddingLeft: depth * 12 }}>
        {hasChildren
          ? <button type="button" tabIndex={-1} className={`jx-canvasOutline-toggle${expanded ? ' is-open' : ''}`}
              aria-label={expanded ? t('收起') : t('展开')} onClick={() => setExpanded((value) => !value)}>
              <RightOutlined />
            </button>
          : <span className="jx-canvasOutline-toggle" aria-hidden="true" />}
        <button type="button" className="jx-canvasOutline-link" title={node.title} onClick={() => onPick(node)}>
          {node.title || t('（无标题）')}
        </button>
      </div>
      {hasChildren && expanded && (
        <ul role="group">
          {node.children.map((child) => <OutlineItem key={child.id} node={child} depth={depth + 1} activeId={activeId} onPick={onPick} />)}
        </ul>
      )}
    </li>
  );
}

/**
 * The last entry at or above the reading line of the document's scroller, or the
 * first one before any is reached. Once a scrolled document hits bottom the line
 * drops to the viewport's end, so short final sections still get their turn.
 */
function readingEntry(nodes: OutlineNode[], body: HTMLElement): string | null {
  const scroller = contentScroller(body);
  const box = scroller.getBoundingClientRect();
  const scrolled = scroller.scrollTop > 0;
  const atEnd = scrolled && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 2;
  const line = atEnd ? box.bottom - READING_LINE : box.top + READING_LINE;
  let current: string | null = null;
  let first: string | null = null;
  const walk = (list: OutlineNode[]) => {
    for (const node of list) {
      const top = node.top?.();
      if (top == null) { walk(node.children); continue; }
      first ??= node.id;
      if (top <= line) current = node.id;
      walk(node.children);
    }
  };
  walk(nodes);
  return current ?? first;
}

/**
 * Header button + a full-height floating outline sidebar inset in the pane's
 * left edge. It floats on the content (never reflows it), selects what was
 * clicked, then follows the section being read, and goes away on Escape, the
 * toggle, or a click anywhere else.
 */
export function CanvasOutline() {
  const tabId = useCanvasPane((s) => s.activeTabId);
  const openSeq = useCanvasPane((s) => s.openSeq);
  const doc = useCanvasDocument(tabId);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const navRef = useRef<HTMLElement>(null);
  const [open, setOpen] = useState(false);
  const surface = usePaneSurface(buttonRef, open, `${tabId}:${openSeq}`);
  const body = surface?.body ?? null;
  const provider = doc?.outline ? doc : body;
  const [result, setResult] = useState<{ provider: unknown; nodes: OutlineNode[] } | null>(null);
  const nodes = result && result.provider === provider ? result.nodes : null;
  const [activeId, setActiveId] = useState<string | null>(null);
  const pickedAt = useRef(0);
  const treeRef = useRef<HTMLUListElement>(null);

  const pick = useCallback((node: OutlineNode) => {
    pickedAt.current = Date.now();
    setActiveId(node.id);
    node.open();
  }, []);

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    if (refocus) buttonRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    if (doc?.outline) {
      void doc.outline()
        .then((found) => { if (!cancelled) setResult({ provider: doc, nodes: found }); })
        .catch(() => { if (!cancelled) setResult({ provider: doc, nodes: [] }); });
      return () => { cancelled = true; };
    }
    if (!body) return;
    // DOM renderers may still be producing content (docx renders async).
    const refresh = () => setResult({ provider: body, nodes: domOutline(body) });
    let timer = window.setTimeout(refresh, 0);
    const observer = new MutationObserver(() => {
      window.clearTimeout(timer);
      timer = window.setTimeout(refresh, 200);
    });
    observer.observe(body, { childList: true, subtree: true });
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      observer.disconnect();
    };
  }, [body, doc, open]);

  // Follow the reading position while the card is open. Scroll events do not
  // bubble, so listen in the capture phase to also hear renderer scrollers (pdf).
  useEffect(() => {
    if (!open || !body || !nodes) return;
    let frame = 0;
    const update = () => {
      frame = 0;
      if (Date.now() - pickedAt.current < PICK_SETTLE_MS) return;
      setActiveId(readingEntry(nodes, body));
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(update); };
    update();
    body.addEventListener('scroll', schedule, { capture: true, passive: true });
    return () => {
      body.removeEventListener('scroll', schedule, { capture: true });
      if (frame) cancelAnimationFrame(frame);
    };
  }, [body, nodes, open]);

  // Keep the marked entry visible inside a long outline.
  useEffect(() => {
    const tree = treeRef.current;
    const row = tree?.querySelector<HTMLElement>('.jx-canvasOutline-row.is-active');
    if (!tree || !row) return;
    const rowBox = row.getBoundingClientRect();
    const treeBox = tree.getBoundingClientRect();
    if (rowBox.top < treeBox.top) tree.scrollTop -= treeBox.top - rowBox.top + 8;
    else if (rowBox.bottom > treeBox.bottom) tree.scrollTop += rowBox.bottom - treeBox.bottom + 8;
  }, [activeId]);

  // A floating card is dismissed by clicking anywhere outside it (the toggle handles itself).
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (navRef.current?.contains(target) || buttonRef.current?.contains(target)) return;
      close(false);
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, [close, open]);

  // Arrow keys move between visible entries; Home/End jump to the ends.
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') { event.preventDefault(); close(true); return; }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const links = Array.from(navRef.current?.querySelectorAll<HTMLButtonElement>('.jx-canvasOutline-link') ?? []);
    if (!links.length) return;
    event.preventDefault();
    const at = links.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === 'Home' ? 0
      : event.key === 'End' ? links.length - 1
        : Math.max(0, Math.min(links.length - 1, at + (event.key === 'ArrowDown' ? 1 : -1)));
    links[next].focus();
  };

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className="jx-canvas-actionBtn"
        aria-label={t('目录')}
        title={t('目录')}
        aria-pressed={open}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <UnorderedListOutlined />
      </button>
      {open && surface && createPortal(
        <nav ref={navRef} className="jx-canvasOutline" aria-label={t('目录')} data-find-skip
          style={{ ['--jx-outline-top' as string]: `${surface.top}px` }} onKeyDown={onKeyDown}>
          <div className="jx-canvasOutline-header">
            <span className="jx-canvasOutline-title">{t('目录')}</span>
            <button type="button" className="jx-canvasOutline-close" aria-label={t('关闭目录')} title={t('关闭目录')}
              onClick={() => close(true)}>
              <CloseOutlined />
            </button>
          </div>
          {nodes === null
            ? <div className="jx-canvasOutline-empty">{t('正在读取目录…')}</div>
            : nodes.length === 0
              ? <div className="jx-canvasOutline-empty">{t('这个文件没有标题或书签')}</div>
              : <ul ref={treeRef} className="jx-canvasOutline-tree" role="tree">
                  {nodes.map((node) => <OutlineItem key={node.id} node={node} depth={0} activeId={activeId} onPick={pick} />)}
                </ul>}
        </nav>,
        surface.pane,
      )}
    </>
  );
}
