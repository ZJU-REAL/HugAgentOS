import { t } from '../../../i18n';
import { HEADING_CANDIDATES, headingLevel } from '../outline/domOutline';

const MAX_HEADING_CHARS = 60;

function precedes(node: Node, point: Node) {
  return node === point || node.contains(point)
    || !!(node.compareDocumentPosition(point) & Node.DOCUMENT_POSITION_FOLLOWING);
}

function headingBefore(container: HTMLElement, point: Node): string | null {
  let nearest: Element | null = null;
  for (const heading of container.querySelectorAll(HEADING_CANDIDATES)) {
    if (!precedes(heading, point)) break;
    if (headingLevel(heading) === null) continue;
    nearest = heading;
  }
  const text = nearest?.textContent?.replace(/\s+/g, ' ').trim();
  if (!text) return null;
  return text.length > MAX_HEADING_CHARS ? `${text.slice(0, MAX_HEADING_CHARS)}…` : text;
}

/**
 * Where in the file a selection sits, as the model and the user should read it:
 * "第 3 页 · 2.1 系统架构". The page comes from the pdf.js page's `data-page`,
 * the heading is the nearest one before the selection. Null when neither exists.
 */
export function describeSelectionLocation(container: HTMLElement, range: Range): string | null {
  const start = range.startContainer;
  const element = start instanceof Element ? start : start.parentElement;
  const page = element?.closest<HTMLElement>('.jx-pdfPage[data-page]')?.dataset.page;
  const parts = [
    page ? t('第 {n} 页', { n: page }) : null,
    headingBefore(container, start),
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : null;
}
