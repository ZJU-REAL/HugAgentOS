import { useState } from 'react';
import { t } from '../../i18n';
import { useUIStore } from '../../stores';
import { resolveAssetUrl, type KBAssetRef } from './kbAssets';

interface KBAssetThumbsProps {
  assets: KBAssetRef[];
  /** Compact variant for the dense tool-result / citation card. */
  compact?: boolean;
}

/**
 * Thumbnail strip for knowledge-base media assets, click to open the full-size preview.
 *
 * The asset endpoint authenticates the caller and resolves it from the `jx_session`
 * cookie, which a plain `<img>` carries. Deployments authenticating by bearer token only
 * get a 401 here, so a failed load drops the thumbnail instead of leaving a broken-image
 * icon in the middle of an answer.
 */
export function KBAssetThumbs({ assets, compact = false }: KBAssetThumbsProps) {
  const setPreviewImage = useUIStore((s) => s.setPreviewImage);
  const [broken, setBroken] = useState<Set<string>>(new Set());

  const visible = (assets || []).filter((a) => a.url && !broken.has(a.url));
  if (!visible.length) return null;

  return (
    <div className={`jx-kbAssetThumbs${compact ? ' jx-kbAssetThumbs--compact' : ''}`}>
      {visible.map((asset) => {
        const src = resolveAssetUrl(asset.url);
        const label = asset.caption || t('文档图片');
        return (
          <img
            key={asset.url}
            src={src}
            alt={label}
            title={label}
            className="jx-kbAssetThumbs-item"
            loading="lazy"
            onError={() => setBroken((prev) => new Set(prev).add(asset.url))}
            onClick={(e) => {
              // Cards are clickable as a whole (open full text) — the thumbnail owns its click.
              e.stopPropagation();
              setPreviewImage({ url: src, name: label });
            }}
          />
        );
      })}
    </div>
  );
}
