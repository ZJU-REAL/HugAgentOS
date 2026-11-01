import { useEffect } from 'react';
import { usePageConfig } from './usePageConfig';

/** Point the browser tab icon at `url` (index.html's static <link rel="icon"> is only a first-frame fallback). */
export function useDocumentFavicon(url: string | undefined): void {
  useEffect(() => {
    if (!url || typeof document === 'undefined') return;
    let link = document.querySelector<HTMLLinkElement>("link[rel~='icon']");
    if (!link) {
      link = document.createElement('link');
      link.rel = 'icon';
      document.head.appendChild(link);
    }
    // The static tag declares type="image/png"; drop it so uploaded SVG / ICO icons are not misread.
    link.removeAttribute('type');
    if (link.getAttribute('href') !== url) link.href = url;
  }, [url]);
}

/** Admin platform pages (/admin, /config, /api-docs) use their own tab icon, falling back to the main-site favicon. */
export function useAdminPlatformFavicon(): void {
  const adminFavicon = usePageConfig('navigation.admin_platform.favicon_url', '');
  const siteFavicon = usePageConfig('branding.favicon_url', '/icon.png');
  useDocumentFavicon(adminFavicon.trim() || siteFavicon.trim() || '/icon.png');
}
