/**
 * Library cards use creation order. Undated catalog entries retain their original
 * order before dated entries; edits do not move an item to the end.
 */
export function sortCapabilitiesByCreation<T extends { created_at?: string | null }>(items: T[]): T[] {
  const timestamp = (item: T): number | null => {
    if (!item.created_at) return null;
    // Database timestamps may omit the UTC suffix; compare them in the same zone as ISO timestamps.
    const iso = /(?:Z|[+-]\d{2}:\d{2})$/i.test(item.created_at) ? item.created_at : `${item.created_at}Z`;
    return Date.parse(iso);
  };
  return [...items].sort((a, b) => {
    const earlier = timestamp(a);
    const later = timestamp(b);
    if (earlier === null) return later === null ? 0 : -1;
    if (later === null) return 1;
    return earlier - later;
  });
}
