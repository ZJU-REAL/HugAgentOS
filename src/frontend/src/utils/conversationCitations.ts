/** One immutable history snapshot shares one index across all message subscribers.
 * Replacing/prepending history creates a new key; old snapshots can be collected.
 */
type Citation = { id: string };
type Entry<T> = { index: number; offset: number; citation: T };
const indexes = new WeakMap<object, Map<string, Entry<Citation>[]>>();

export function resolveIndexedCitations<T extends Citation>(
  messages: ReadonlyArray<{ uid: string; citations?: readonly T[] }>,
  currentIndex: number,
  own: T[],
  referencedIds: readonly string[],
): T[] {
  const present = new Set(own.map(c => c.id));
  const missing = referencedIds.filter(id => !present.has(id));
  if (!missing.length) return own;
  let index = indexes.get(messages);
  if (!index) {
    index = new Map();
    messages.forEach((message, position) => {
      message.citations?.forEach((citation, offset) => {
        const entries = index!.get(citation.id) ?? [];
        entries.push({ index: position, offset, citation });
        index!.set(citation.id, entries);
      });
    });
    indexes.set(messages, index);
  }
  const found: Entry<Citation>[] = [];
  for (const id of new Set(missing)) {
    const entries = index.get(id);
    if (!entries) continue;
    let low = 0, high = entries.length;
    while (low < high) {
      const middle = (low + high) >>> 1;
      if (entries[middle].index < currentIndex) low = middle + 1;
      else high = middle;
    }
    if (low === 0) continue;
    // Preserve the original first-citation-wins rule within the latest message.
    let at = low - 1;
    while (at > 0 && entries[at - 1].index === entries[at].index) at--;
    found.push(entries[at]);
  }
  found.sort((a, b) => b.index - a.index || a.offset - b.offset);
  return found.length ? [...own, ...found.map(e => e.citation as T)] : own;
}
