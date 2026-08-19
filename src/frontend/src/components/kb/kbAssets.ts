import { getApiUrl } from '../../api';

/** Asset links written into chunk text by indexing: `![](/api/v1/catalog/kb/assets/<id>)`. */
const ASSET_LINK_RE = /!\[[^\]]*\]\(([^)\s]*\/v1\/catalog\/kb\/assets\/[^)\s]+)\)/g;

export interface KBAssetRef {
  url: string;
  caption?: string;
  asset_id?: string;
}

/**
 * Pull asset references out of stored chunk text.
 *
 * Used where only the text is at hand (the chunk viewer). Retrieval results carry a
 * structured `images` array instead — prefer that, it also brings the caption.
 */
export function extractAssetRefs(content: string): KBAssetRef[] {
  const refs: KBAssetRef[] = [];
  for (const match of (content || '').matchAll(ASSET_LINK_RE)) {
    const url = match[1];
    if (url && !refs.some((r) => r.url === url)) refs.push({ url });
  }
  return refs;
}

/**
 * Resolve a stored asset URL against the running API base.
 *
 * Stored links carry the `/api` prefix (`KB_ASSET_URL_PREFIX`), which is also the default
 * API base — strip it before re-prefixing so a deployment serving the API elsewhere still
 * resolves.
 */
export function resolveAssetUrl(url: string): string {
  if (!url) return '';
  if (url.startsWith('http')) return url;
  return `${getApiUrl()}${url.replace(/^\/api/, '')}`;
}
