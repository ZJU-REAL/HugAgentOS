import { useEffect } from 'react';
import { usePageConfigAll } from './usePageConfig';
import type { PageConfig } from '../utils/pageConfigDefaults';

/** Point the browser tab icon at `url`. index.html ships no icon of its own, so the configured one is the first one shown. */
export function setDocumentFavicon(url: string): void {
  if (!url || typeof document === 'undefined') return;
  let link = document.querySelector<HTMLLinkElement>("link[rel~='icon']");
  if (!link) {
    link = document.createElement('link');
    link.rel = 'icon';
    document.head.appendChild(link);
  }
  // Drop any declared type so uploaded SVG / ICO icons are not misread.
  link.removeAttribute('type');
  if (link.getAttribute('href') !== url) link.href = url;
}

export function useDocumentFavicon(url: string | undefined): void {
  useEffect(() => { if (url) setDocumentFavicon(url); }, [url]);
}

/** The tab icon for the site, or for the admin platform pages (/admin, /config, /api-docs), which may set their own. */
export function resolveDocumentFavicon(config: PageConfig, adminPage: boolean): string {
  return (adminPage && config.navigation.admin_platform.favicon_url.trim()) || config.branding.favicon_url.trim() || '/icon.png';
}

/** Admin platform pages use their own tab icon, falling back to the main-site favicon. */
export function useAdminPlatformFavicon(): void {
  useDocumentFavicon(resolveDocumentFavicon(usePageConfigAll(), true));
}
