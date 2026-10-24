import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { CheckOutlined, CopyOutlined } from '@ant-design/icons';
import { t } from '../../../i18n';
import { copyToClipboard } from '../../../utils/clipboard';
import './SelectionToolbar.css';

/** The selection an action runs on: trimmed text, the DOM range, and the container it was made in. */
export interface SelectionSnapshot {
  text: string;
  range: Range;
  container: HTMLElement;
}

export interface SelectionAction {
  key: string;
  label: string;
  icon: ReactNode;
  run: (selection: SelectionSnapshot) => void;
}

interface ToolbarPosition {
  x: number;
  y: number;
  below: boolean;
}

/** Room the toolbar needs above the selection before it flips below it. */
const TOOLBAR_CLEARANCE = 56;
const VIEWPORT_MARGIN = 16;

/** Selections inside editable fields belong to the editor, not to this toolbar. */
function rangeInside(element: HTMLElement | null, range: Range) {
  if (!element || !(element.contains(range.startContainer) || element.contains(range.endContainer))) return false;
  const start = range.startContainer instanceof Element ? range.startContainer : range.startContainer.parentElement;
  return !start?.closest('[contenteditable="true"], textarea, input');
}

/**
 * Floating toolbar for text selected inside one container: copy is built in, the
 * caller adds its own actions. The hook owns everything between "the user let go of
 * the mouse" and "an action ran" — keeping the selection alive across React
 * re-renders and toolbar clicks, following scroll, and hiding when the selection
 * collapses or leaves the container.
 */
export function useSelectionToolbar({ actions, enabled = true }: { actions: SelectionAction[]; enabled?: boolean }) {
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const containerRef = useRef<HTMLElement | null>(null);
  const rangeRef = useRef<Range | null>(null);
  const pointerDownRef = useRef<{ x: number; y: number; hadSelection: boolean } | null>(null);
  // selectionchange fires (collapsed) while React re-renders or a toolbar button takes
  // the mousedown; ignore it for a moment instead of tearing the toolbar down.
  const guardUntilRef = useRef(0);
  const shownRef = useRef(false);
  const copiedTimerRef = useRef<number | null>(null);
  const [position, setPosition] = useState<ToolbarPosition | null>(null);
  const [copied, setCopied] = useState(false);

  const ref = useCallback((element: HTMLElement | null) => {
    containerRef.current = element;
    setContainer(element);
  }, []);

  const guard = () => { guardUntilRef.current = Date.now() + 80; };

  const hide = useCallback(() => {
    shownRef.current = false;
    rangeRef.current = null;
    setPosition(null);
    setCopied(false);
  }, []);

  const restoreRange = () => {
    const selection = window.getSelection();
    if (!rangeRef.current || !selection) return;
    selection.removeAllRanges();
    selection.addRange(rangeRef.current);
  };

  const show = useCallback((fallback?: Range | null) => {
    const selection = window.getSelection();
    const live = selection && selection.rangeCount > 0 && !selection.isCollapsed ? selection.getRangeAt(0) : null;
    const range = (live ?? fallback)?.cloneRange() ?? null;
    if (!range || !range.toString().trim() || !rangeInside(containerRef.current, range)) {
      hide();
      return;
    }
    const rect = range.getBoundingClientRect();
    if (!rect.width && !rect.height) {
      hide();
      return;
    }
    rangeRef.current = range;
    const below = rect.top < TOOLBAR_CLEARANCE;
    const x = Math.min(Math.max(rect.left + rect.width / 2, VIEWPORT_MARGIN), window.innerWidth - VIEWPORT_MARGIN);
    shownRef.current = true;
    guard();
    setPosition({ x, y: below ? rect.bottom + 12 : rect.top - 12, below });
  }, [hide]);

  // A React re-render (the toolbar appearing) can drop the browser selection; put it back.
  useLayoutEffect(() => {
    if (!position || !rangeRef.current) return;
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) {
      guard();
      restoreRange();
    }
  }, [position]);

  useEffect(() => {
    if (!container || !enabled) return;
    const onMouseDown = (event: MouseEvent) => {
      if (event.button !== 0) return;
      const selection = window.getSelection();
      const current = selection && selection.rangeCount > 0 && !selection.isCollapsed ? selection.getRangeAt(0) : null;
      pointerDownRef.current = { x: event.clientX, y: event.clientY, hadSelection: !!current && rangeInside(container, current) };
    };
    const onMouseUp = (event: MouseEvent) => {
      const down = pointerDownRef.current;
      pointerDownRef.current = null;
      // A plain click on an existing selection dismisses it.
      if (down?.hadSelection && Math.abs(event.clientX - down.x) <= 3 && Math.abs(event.clientY - down.y) <= 3) {
        guardUntilRef.current = 0;
        window.getSelection()?.removeAllRanges();
        hide();
        return;
      }
      // Capture now: a re-render before the timeout may already have destroyed the selection.
      const selection = window.getSelection();
      const captured = selection && selection.rangeCount > 0 && !selection.isCollapsed ? selection.getRangeAt(0).cloneRange() : null;
      if (captured) guard();
      window.setTimeout(() => show(captured), 0);
    };
    container.addEventListener('mousedown', onMouseDown);
    container.addEventListener('mouseup', onMouseUp);
    return () => {
      container.removeEventListener('mousedown', onMouseDown);
      container.removeEventListener('mouseup', onMouseUp);
    };
  }, [container, enabled, hide, show]);

  useEffect(() => {
    const onSelectionChange = () => {
      const selection = window.getSelection();
      if ((!selection || selection.isCollapsed) && shownRef.current && Date.now() >= guardUntilRef.current) hide();
    };
    const follow = () => { if (shownRef.current && rangeRef.current) show(rangeRef.current); };
    document.addEventListener('selectionchange', onSelectionChange);
    window.addEventListener('scroll', follow, true);
    window.addEventListener('resize', follow);
    return () => {
      if (copiedTimerRef.current) window.clearTimeout(copiedTimerRef.current);
      document.removeEventListener('selectionchange', onSelectionChange);
      window.removeEventListener('scroll', follow, true);
      window.removeEventListener('resize', follow);
    };
  }, [hide, show]);

  const snapshot = (): SelectionSnapshot | null => {
    const range = rangeRef.current;
    const container = containerRef.current;
    const text = (window.getSelection()?.toString() || range?.toString() || '').trim();
    return range && container && text ? { text, range, container } : null;
  };

  const copy = async (selection: SelectionSnapshot) => {
    const ok = await copyToClipboard(selection.text);
    restoreRange();
    if (!ok) return;
    setCopied(true);
    if (copiedTimerRef.current) window.clearTimeout(copiedTimerRef.current);
    copiedTimerRef.current = window.setTimeout(() => {
      setCopied(false);
      copiedTimerRef.current = null;
    }, 1800);
  };

  const trigger = (event: React.MouseEvent, run: (selection: SelectionSnapshot) => void) => {
    // mousedown, not click: by click time the browser has already collapsed the selection.
    event.preventDefault();
    event.stopPropagation();
    guard();
    restoreRange();
    const selection = snapshot();
    if (selection) run(selection);
  };

  const toolbar = enabled && position ? createPortal(
    <div
      className={`jx-selectionToolbar${position.below ? ' jx-selectionToolbar--below' : ''}`}
      style={{ left: position.x, top: position.y }}
      role="toolbar"
      aria-label={t('选中内容操作')}
    >
      <button
        type="button"
        className={`jx-selectionToolbarBtn${copied ? ' copied' : ''}`}
        title={copied ? t('已复制') : t('复制')}
        onMouseDown={(event) => trigger(event, (selection) => { void copy(selection); })}
      >
        <span className="jx-selectionToolbarIcon">{copied ? <CheckOutlined /> : <CopyOutlined />}</span>
        <span className="jx-selectionToolbarLabel">{copied ? t('已复制') : t('复制')}</span>
      </button>
      {actions.map((action) => (
        <button
          key={action.key}
          type="button"
          className="jx-selectionToolbarBtn"
          title={action.label}
          onMouseDown={(event) => trigger(event, (selection) => {
            action.run(selection);
            // The action has taken the selection elsewhere (usually the composer).
            hide();
          })}
        >
          <span className="jx-selectionToolbarIcon">{action.icon}</span>
          <span className="jx-selectionToolbarLabel">{action.label}</span>
        </button>
      ))}
    </div>,
    document.body,
  ) : null;

  return { ref, toolbar };
}
