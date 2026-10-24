import { scrollIntoPane, type OutlineNode } from '../canvasDocument';

// docx-preview (default className "docx") renders Word headings as
// <p class="docx_<styleId>">: English templates use "Heading1", Chinese ones the
// bare level ("1" = 标题 1).
const DOCX_HEADING = /^docx_(?:heading)?([1-6])$/i;

/** Selector for every element `headingLevel` may recognise. */
export const HEADING_CANDIDATES = 'h1, h2, h3, h4, h5, h6, p[class*="docx_"]';

/** Heading level of a rendered markdown/HTML tag or docx-preview paragraph, else null. */
export function headingLevel(el: Element): number | null {
  const tag = /^H([1-6])$/.exec(el.tagName);
  if (tag) return Number(tag[1]);
  for (const name of el.classList) {
    const match = DOCX_HEADING.exec(name);
    if (match) return Number(match[1]);
  }
  return null;
}

/** One-shot tint on the heading a jump landed on, so the jump reads even when nothing had to scroll. */
function flash(el: Element) {
  el.classList.remove('jx-anim-flash');
  void (el as HTMLElement).offsetWidth; // restart the animation on repeated clicks
  el.classList.add('jx-anim-flash');
  el.addEventListener('animationend', () => el.classList.remove('jx-anim-flash'), { once: true });
}

/** Heading tree of rendered DOM (markdown, docx, editors). */
export function domOutline(root: HTMLElement): OutlineNode[] {
  const entries = Array.from(root.querySelectorAll(HEADING_CANDIDATES))
    .map((el) => ({ el, level: headingLevel(el) }))
    .filter((entry): entry is { el: Element; level: number } => entry.level !== null && !!entry.el.textContent?.trim());

  const roots: OutlineNode[] = [];
  const stack: Array<{ level: number; node: OutlineNode }> = [];
  entries.forEach(({ el, level }, index) => {
    const node: OutlineNode = {
      id: `dom-${index}`,
      title: el.textContent?.trim().replace(/\s+/g, ' ') ?? '',
      children: [],
      open: () => {
        scrollIntoPane(el, 'start');
        flash(el);
      },
      top: () => (el.isConnected ? el.getBoundingClientRect().top : null),
    };
    while (stack.length && stack[stack.length - 1].level >= level) stack.pop();
    (stack.length ? stack[stack.length - 1].node.children : roots).push(node);
    stack.push({ level, node });
  });
  return roots;
}
