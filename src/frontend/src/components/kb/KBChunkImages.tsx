import { useMemo } from 'react';
import { KBAssetThumbs } from './KBAssetThumbs';
import { extractAssetRefs } from './kbAssets';

interface KBChunkImagesProps {
  /** Parent-chunk text as stored; asset links are extracted from it. */
  content: string;
}

/**
 * Figures carried by a chunk, for the knowledge-base chunk viewer.
 *
 * The chunk body renders as plain text, so without this the asset links show up as
 * literal markdown. Sourcing them from the text keeps this purely presentational — no
 * extra request, and it stays correct as long as indexing writes the links.
 */
export function KBChunkImages({ content }: KBChunkImagesProps) {
  const assets = useMemo(() => extractAssetRefs(content), [content]);
  return <KBAssetThumbs assets={assets} />;
}
