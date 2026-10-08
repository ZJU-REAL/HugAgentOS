import { useEffect, useLayoutEffect, useRef, useState, type MouseEvent } from 'react';
import { createPortal } from 'react-dom';
import { CheckOutlined, CopyOutlined, RedoOutlined } from '@ant-design/icons';
import { useComposerDraft, chatDraftKey } from '../../stores/composerStore';
import type { ChatMessage } from '../../types';
import { t } from '../../i18n';
export function useMessageSelection(m: ChatMessage, chatId: string) {
  const { setQuotedFollowUp } = useComposerDraft(chatDraftKey(chatId));
  const contentRef = useRef<HTMLDivElement | null>(null);
  const selectionRangeRef = useRef<Range | null>(null);
  const selectionCopiedTimerRef = useRef<number | null>(null);
  const selectionCopiedTextRef = useRef<string | null>(null);
  const selectionPointerDownRef = useRef<{ x: number; y: number; hadSelection: boolean } | null>(null);
  const selectionGuardUntilRef = useRef(0);
  const selectionToolbarRef = useRef<{ x: number; y: number; text: string } | null>(null);
  const [selectionToolbar, setSelectionToolbar] = useState<{ x: number; y: number; text: string } | null>(null);
  const [selectionCopied, setSelectionCopied] = useState(false);
  const hideSelectionToolbar = () => {
    selectionToolbarRef.current = null;
    selectionCopiedTextRef.current = null;
    setSelectionToolbar(null);
    setSelectionCopied(false);
  };

  const guardSelectionState = () => {
    selectionGuardUntilRef.current = Date.now() + 80;
  };

  const restoreSelectionRange = () => {
    if (!selectionRangeRef.current) return;
    const selection = window.getSelection();
    if (!selection) return;
    selection.removeAllRanges();
    selection.addRange(selectionRangeRef.current);
  };

  const hasSelectionInsideContent = () => {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed || !contentRef.current) {
      return false;
    }
    const range = selection.getRangeAt(0);
    return contentRef.current.contains(range.startContainer) || contentRef.current.contains(range.endContainer);
  };

  const clearSelectionState = () => {
    selectionGuardUntilRef.current = 0;
    selectionRangeRef.current = null;
    const selection = window.getSelection();
    if (selection) {
      selection.removeAllRanges();
    }
    hideSelectionToolbar();
  };

  const getStoredSelectionText = () => {
    const current = window.getSelection()?.toString().trim();
    if (current) return current;
    const stored = selectionRangeRef.current?.toString().trim();
    return stored || '';
  };

  /**
   * Check if the current window selection overlaps with this message's
   * contentRef and, if so, show the toolbar.  Called from both mouseup
   * and selectionchange so we catch every path (mouse, keyboard, touch).
   */
  const checkAndShowToolbar = (fallbackRange?: Range | null) => {
    const selection = window.getSelection();
    let range: Range | null = null;
    let selectedText = '';

    if (selection && selection.rangeCount > 0 && !selection.isCollapsed) {
      range = selection.getRangeAt(0).cloneRange();
      selectedText = selection.toString().trim();
    } else if (fallbackRange) {
      range = fallbackRange.cloneRange();
      selectedText = range.toString().trim();
    }

    if (!range || !selectedText) {
      hideSelectionToolbar();
      selectionRangeRef.current = null;
      return;
    }

    // At least one endpoint of the selection must be inside our content
    const anchorNode = selection && !selection.isCollapsed ? selection.anchorNode : range.startContainer;
    const focusNode = selection && !selection.isCollapsed ? selection.focusNode : range.endContainer;
    if (!contentRef.current || !anchorNode || !focusNode) {
      hideSelectionToolbar();
      selectionRangeRef.current = null;
      return;
    }

    const anchorInside = contentRef.current.contains(anchorNode);
    const focusInside = contentRef.current.contains(focusNode);
    if (!anchorInside && !focusInside) {
      // Selection is entirely outside this bubble — ignore
      hideSelectionToolbar();
      selectionRangeRef.current = null;
      return;
    }

    selectionRangeRef.current = range.cloneRange();
    const rect = range.getBoundingClientRect();
    if (!rect.width && !rect.height) {
      hideSelectionToolbar();
      selectionRangeRef.current = null;
      return;
    }

    const pos = { x: rect.left + rect.width / 2, y: rect.top - 12, text: selectedText };
    selectionToolbarRef.current = pos;
    guardSelectionState();
    if (selectionCopiedTextRef.current !== selectedText) {
      setSelectionCopied(false);
    }
    setSelectionToolbar(pos);
  };

  const handleSelectionFollowUpQuote = () => {
    const quoteText = getStoredSelectionText() || selectionToolbar?.text || '';
    if (!quoteText) return;
    setQuotedFollowUp({ text: quoteText, ts: m.ts });
    restoreSelectionRange();
  };

  const handleSelectionToolbarMouseDown = (
    e: MouseEvent<HTMLButtonElement>,
    action: () => void,
  ) => {
    e.preventDefault();
    e.stopPropagation();
    guardSelectionState();
    restoreSelectionRange();
    action();
  };

  // Defensive: restore selection after React re-render caused by toolbar state update.
  // When setSelectionToolbar(pos) triggers a re-render, the DOM reconciliation may
  // cause the browser to lose the active selection. This effect restores it.
  useLayoutEffect(() => {
    if (selectionToolbar && selectionRangeRef.current) {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) {
        guardSelectionState();
        restoreSelectionRange();
      }
    }
  }, [selectionToolbar]);

  // ---------- Selection event listeners ----------
  // Registered ONCE ([] deps) so listeners are never torn down and re-added.
  // This avoids the race where React's async effect cleanup removes the
  // selectionchange listener while a pending selectionchange event fires
  // and clears the selection.
  useEffect(() => {
    const handleSelectionChange = () => {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed) {
        if (Date.now() < selectionGuardUntilRef.current) {
          return;
        }
        // Only update state if toolbar is currently shown (avoid unnecessary re-renders)
        if (selectionToolbarRef.current) {
          selectionToolbarRef.current = null;
          selectionRangeRef.current = null;
          setSelectionToolbar(null);
          setSelectionCopied(false);
        }
      }
    };

    const handleWindowScroll = () => {
      // Read ref (always current) instead of closed-over state
      if (selectionToolbarRef.current && selectionRangeRef.current) {
        checkAndShowToolbar(selectionRangeRef.current);
      }
    };

    document.addEventListener('selectionchange', handleSelectionChange);
    window.addEventListener('scroll', handleWindowScroll, true);
    window.addEventListener('resize', handleWindowScroll);

    return () => {
      if (selectionCopiedTimerRef.current) {
        window.clearTimeout(selectionCopiedTimerRef.current);
      }
      document.removeEventListener('selectionchange', handleSelectionChange);
      window.removeEventListener('scroll', handleWindowScroll, true);
      window.removeEventListener('resize', handleWindowScroll);
    };
  }, []);

  const doSelectionCopy = (raw: string) => {
    const str = getStoredSelectionText() || raw;
    if (!str) return;
    const markSelectionCopied = () => {
      selectionCopiedTextRef.current = str;
      setSelectionCopied(true);
      if (selectionCopiedTimerRef.current) {
        window.clearTimeout(selectionCopiedTimerRef.current);
      }
      selectionCopiedTimerRef.current = window.setTimeout(() => {
        selectionCopiedTextRef.current = null;
        setSelectionCopied(false);
        selectionCopiedTimerRef.current = null;
      }, 1800);
    };

    const copyFallback = (s: string) => {
      const ta = document.createElement('textarea');
      ta.value = s;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      restoreSelectionRange();
      markSelectionCopied();
    };

    if (navigator.clipboard) {
      navigator.clipboard.writeText(str).then(() => {
        restoreSelectionRange();
        markSelectionCopied();
      }).catch(() => copyFallback(str));
    } else {
      copyFallback(str);
    }
  };

  const onMouseDown = (e: MouseEvent<HTMLDivElement>) => {
          if (e.button !== 0) return;
          selectionPointerDownRef.current = {
            x: e.clientX,
            y: e.clientY,
            hadSelection: hasSelectionInsideContent(),
          };
  };
  const onMouseUp = (e: MouseEvent<HTMLDivElement>) => {
          const pointerDown = selectionPointerDownRef.current;
          selectionPointerDownRef.current = null;
          if (pointerDown?.hadSelection) {
            const moved = Math.abs(e.clientX - pointerDown.x) > 3 || Math.abs(e.clientY - pointerDown.y) > 3;
            if (!moved) {
              clearSelectionState();
              return;
            }
          }

          // Save selection range immediately before any async processing,
          // so it can be restored if a React re-render destroys the selection.
          const sel = window.getSelection();
          let capturedRange: Range | null = null;
          if (sel && sel.rangeCount > 0 && !sel.isCollapsed) {
            capturedRange = sel.getRangeAt(0).cloneRange();
            selectionRangeRef.current = capturedRange;
            guardSelectionState();
          }
          window.setTimeout(() => checkAndShowToolbar(capturedRange), 0);
  };
  const selectionMenu = !m.isStreaming && selectionToolbar ? createPortal(
          <div
            className="jx-selectionToolbar"
            style={{ left: selectionToolbar.x, top: selectionToolbar.y }}
          >
            <button
              type="button"
              className={`jx-selectionToolbarBtn${selectionCopied ? ' copied' : ''}`}
              title={selectionCopied ? t('已复制') : t('复制')}
              onMouseDown={(e) => handleSelectionToolbarMouseDown(e, () => {
                doSelectionCopy(selectionToolbar.text);
              })}
            >
              <span className="jx-selectionToolbarIcon">{selectionCopied ? <CheckOutlined /> : <CopyOutlined />}</span>
              <span className="jx-selectionToolbarLabel">{selectionCopied ? t('已复制') : t('复制')}</span>
            </button>
            <button
              type="button"
              className="jx-selectionToolbarBtn"
              title={t('追问')}
              onMouseDown={(e) => handleSelectionToolbarMouseDown(e, handleSelectionFollowUpQuote)}
            >
              <span className="jx-selectionToolbarIcon"><RedoOutlined /></span>
              <span className="jx-selectionToolbarLabel">{t('追问')}</span>
            </button>
          </div>,
          document.body,
  ) : null;
  return { contentRef, onMouseDown, onMouseUp, selectionMenu };
}
