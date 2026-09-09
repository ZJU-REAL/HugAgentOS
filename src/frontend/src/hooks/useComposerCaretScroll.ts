import { useEffect, type RefObject } from 'react';

/** Scroll only the editor: its overlaid toolbar is not part of the browser's caret viewport. */
export function useComposerCaretScroll(editorRef: RefObject<HTMLDivElement | null>) {
  useEffect(() => {
    const editor = editorRef.current;
    if (!editor) return;
    let frame = 0;

    const revealCaret = () => {
      frame = 0;
      const selection = window.getSelection();
      if (document.activeElement !== editor || !selection?.isCollapsed || !selection.rangeCount) return;
      const range = selection.getRangeAt(0).cloneRange();
      if (!editor.contains(range.startContainer)) return;

      let caret = range.getBoundingClientRect();
      // Chromium can leave the caret in an empty text node after Shift+Enter.
      // Measure the adjacent line break without inserting nodes or changing the IME selection.
      if (!caret.height) {
        const node = range.startContainer;
        const next = node.nodeType === Node.TEXT_NODE
          ? node : node.childNodes[range.startOffset];
        if (next?.nodeType === Node.TEXT_NODE && (next.textContent?.length || 0) > range.startOffset) {
          const offset = node.nodeType === Node.TEXT_NODE ? range.startOffset : 0;
          range.setStart(next, offset);
          range.setEnd(next, offset + 1);
        } else if (next instanceof HTMLBRElement) {
          range.selectNode(next);
        } else {
          let previous: Node | null = node.nodeType === Node.TEXT_NODE
            ? node.previousSibling : node.childNodes[range.startOffset - 1] || null;
          while (previous && !previous.textContent && !(previous instanceof HTMLBRElement)) {
            previous = previous.previousSibling;
          }
          if (!previous) return;
          while (previous.lastChild) previous = previous.lastChild;
          if (previous.nodeType === Node.TEXT_NODE && previous.textContent?.length) {
            range.setStart(previous, previous.textContent.length - 1);
            range.setEnd(previous, previous.textContent.length);
          } else {
            range.selectNode(previous);
          }
        }
        caret = range.getBoundingClientRect();
      }
      if (!caret.height) return;

      const box = editor.getBoundingClientRect();
      const toolbar = editor.parentElement?.querySelector<HTMLElement>('.jx-composerBar');
      const top = box.top + editor.clientTop + 4;
      // Include the toolbar's fade so the active line stays fully readable.
      const bottom = Math.min(box.top + editor.clientTop + editor.clientHeight,
        toolbar ? toolbar.getBoundingClientRect().top - 12 : box.bottom) - 4;
      if (bottom <= top) return;
      if (caret.bottom > bottom) editor.scrollTop += caret.bottom - bottom;
      else if (caret.top < top) editor.scrollTop -= top - caret.top;
    };
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(revealCaret);
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) {
        schedule();
      }
    };
    // Pointer releases include dragging the scrollbar; they must not snap back to the caret.
    const events = ['input', 'focus', 'compositionend'] as const;
    editor.addEventListener('keyup', onKeyUp);
    events.forEach(event => editor.addEventListener(event, schedule));
    const observer = new ResizeObserver(schedule);
    observer.observe(editor);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      editor.removeEventListener('keyup', onKeyUp);
      events.forEach(event => editor.removeEventListener(event, schedule));
    };
  }, [editorRef]);
}
