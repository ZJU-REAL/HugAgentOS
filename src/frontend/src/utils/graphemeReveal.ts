import { morphChildren, type TextPatch } from './domPatch';

const TEXT_CONTAINER = 'data-jx-reveal-text';
const SKIP_TEXT = 'pre,code,button,svg,math,.katex,[data-jxcit],[data-chart]';
const segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
/** Beyond this many new graphemes in one patch the text is shown at once. */
const MAX_REVEAL_GRAPHEMES = 2000;

function sharedPrefix(a: string, b: string): number {
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i += 1;
  return i;
}

function hasSelection(el: Node): boolean {
  const selection = el.ownerDocument?.getSelection();
  if (!selection || selection.isCollapsed) return false;
  for (let i = 0; i < selection.rangeCount; i += 1) {
    if (selection.getRangeAt(i).intersectsNode(el)) return true;
  }
  return false;
}

/** Cut only a changed suffix; the stable text nodes retain browser selections. */
function trimSuffix(el: HTMLElement, length: number) {
  const walker = el.ownerDocument.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  let remaining = length;
  while (node) {
    const size = node.textContent?.length ?? 0;
    if (remaining <= size) {
      const range = el.ownerDocument.createRange();
      range.setStart(node, remaining);
      range.setEnd(el, el.childNodes.length);
      range.deleteContents();
      return;
    }
    remaining -= size;
    node = walker.nextNode();
  }
}

/** One display controller per message body. It never owns protocol or persisted state. */
export class GraphemeReveal {
  private previousText = '';
  private snapshot: number | undefined;
  private nextStart = 0;
  private pending = new Set<HTMLElement>();

  private finish(span: HTMLElement) {
    this.pending.delete(span);
    span.removeAttribute('class');
    span.removeAttribute('style');
    const parent = span.parentElement;
    // Removing a selected node invalidates the browser's range, even if its
    // replacement has identical text. Keep that node until a later render.
    if (!parent || hasSelection(span)) return;
    span.replaceWith(span.ownerDocument.createTextNode(span.textContent ?? ''));
    if (parent.childElementCount === 0 && !hasSelection(parent)) parent.normalize();
  }

  settle() {
    for (const span of [...this.pending]) this.finish(span);
    this.nextStart = 0;
  }

  patch(container: HTMLElement, html: string, streaming: boolean, snapshot?: number) {
    const template = container.ownerDocument.createElement('template');
    template.innerHTML = html;
    const starts = new WeakMap<Node, number>();
    let nextText = '';
    const walker = container.ownerDocument.createTreeWalker(template.content, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.textContent?.trim() || node.parentElement?.closest(SKIP_TEXT)) continue;
      starts.set(node, nextText.length);
      nextText += node.textContent;
    }
    const reduced = container.ownerDocument.defaultView?.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const restored = snapshot !== undefined && snapshot !== this.snapshot;
    let revealing = streaming && !reduced && !restored;
    let prefix = revealing ? sharedPrefix(this.previousText, nextText) : nextText.length;
    let count = revealing ? [...segmenter.segment(nextText.slice(prefix))].length : 0;
    // A body mounted mid-stream (e.g. a panel opened on a long run) would wrap
    // everything received so far in animated spans and lock the main thread.
    if (count > MAX_REVEAL_GRAPHEMES) {
      revealing = false;
      prefix = nextText.length;
      count = 0;
    }
    if (!revealing) this.settle();
    for (const span of this.pending) if (!span.isConnected) this.pending.delete(span);

    const style = getComputedStyle(container);
    const stagger = parseFloat(style.getPropertyValue('--jx-reveal-stagger'));
    const backlog = parseFloat(style.getPropertyValue('--jx-reveal-backlog'));
    const now = performance.now();
    let cursor = Math.min(now + backlog, Math.max(now, this.nextStart));
    // Keep normal 30ms pacing, but catch up for large server bursts. The
    // animation backlog is bounded independently of model/network speed.
    const interval = Math.min(stagger, Math.max(0, (backlog - (cursor - now)) / Math.max(1, count - 1)));

    const textPatch: TextPatch = (oldNode, newNode) => {
      const start = starts.get(newNode);
      if (start === undefined) return undefined;
      const text = newNode.data;
      const el = oldNode instanceof HTMLElement && oldNode.hasAttribute(TEXT_CONTAINER)
        ? oldNode : container.ownerDocument.createElement('span');
      el.setAttribute(TEXT_CONTAINER, '');
      const previous = el.textContent ?? '';
      if (previous === text) return el;
      if (!revealing || start + text.length <= prefix) {
        if (text.startsWith(previous)) el.append(container.ownerDocument.createTextNode(text.slice(previous.length)));
        else el.textContent = text;
        if (!hasSelection(el)) el.normalize();
        return el;
      }
      const segments = [...segmenter.segment(text)];
      const common = sharedPrefix(previous, text);
      // A network chunk can end inside a combining sequence or ZWJ emoji.
      // Rebuild that final grapheme rather than exposing a detached suffix.
      let stable = 0;
      for (const part of segments) {
        const end = part.index + part.segment.length;
        if (end > common) break;
        stable = end;
      }
      trimSuffix(el, stable);
      const fragment = container.ownerDocument.createDocumentFragment();
      for (const part of segments) {
        if (part.index < stable) continue;
        if (!revealing || start + part.index < prefix) {
          fragment.append(container.ownerDocument.createTextNode(part.segment));
          continue;
        }
        const span = container.ownerDocument.createElement('span');
        span.className = 'jx-grapheme';
        span.textContent = part.segment;
        span.style.setProperty('--jx-reveal-delay', `${Math.max(0, cursor - now)}ms`);
        cursor += interval;
        this.pending.add(span);
        span.addEventListener('animationend', () => this.finish(span), { once: true });
        fragment.append(span);
      }
      el.append(fragment);
      return el;
    };
    morphChildren(container, template.content, textPatch);
    this.nextStart = cursor;
    this.previousText = nextText;
    this.snapshot = snapshot;
  }
}
