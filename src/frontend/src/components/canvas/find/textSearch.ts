export const MAX_FIND_HITS = 2000;

const fold = (text: string) => {
  const lower = text.toLowerCase();
  // Case folding must keep offsets aligned with the source text.
  return lower.length === text.length ? lower : text;
};

/** Start offsets of every non-overlapping, case-insensitive occurrence. */
export function findOffsets(text: string, query: string, limit = MAX_FIND_HITS): number[] {
  const needle = fold(query);
  if (!needle) return [];
  const haystack = fold(text);
  const offsets: number[] = [];
  for (let at = haystack.indexOf(needle); at !== -1 && offsets.length < limit; at = haystack.indexOf(needle, at + needle.length)) {
    offsets.push(at);
  }
  return offsets;
}

const SKIPPED = 'script, style, noscript, textarea, [aria-hidden="true"], [data-find-skip]';

const BLOCK = 'p, div, li, dt, dd, h1, h2, h3, h4, h5, h6, td, th, tr, pre, blockquote, section, article, table';

/**
 * Concatenated text of `root` plus the text node that starts at each offset.
 * A newline separates text from different blocks (paragraphs, code lines), so
 * matches never join the end of one line to the start of the next.
 */
function collectText(root: Node) {
  const nodes: Text[] = [];
  const starts: number[] = [];
  let text = '';
  let block: Element | null | undefined;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => node.parentElement?.closest(SKIPPED) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    if (!node.data) continue;
    const owner = node.parentElement?.closest(BLOCK) ?? null;
    if (block !== undefined && owner !== block) text += '\n';
    block = owner;
    nodes.push(node);
    starts.push(text.length);
    text += node.data;
  }
  return { nodes, starts, text };
}

function locate(starts: number[], offset: number): number {
  let lo = 0;
  let hi = starts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (starts[mid] <= offset) lo = mid; else hi = mid - 1;
  }
  return lo;
}

/**
 * Ranges for each occurrence of `query` in the rendered text under `root`.
 * Matches may cross element boundaries (highlighted code tokens, pdf runs).
 */
export function findTextRanges(root: Node, query: string, limit = MAX_FIND_HITS): Range[] {
  if (!query) return [];
  const { nodes, starts, text } = collectText(root);
  return findOffsets(text, query, limit).map((offset) => {
    const end = offset + query.length;
    const first = locate(starts, offset);
    const last = locate(starts, end - 1);
    const range = document.createRange();
    range.setStart(nodes[first], offset - starts[first]);
    range.setEnd(nodes[last], end - starts[last]);
    return range;
  });
}
