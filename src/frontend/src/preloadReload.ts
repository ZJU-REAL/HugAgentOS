/**
 * After a release, an old tab still holds the old entry chunk; lazy-loading a hash file
 * already removed by the new build throws "Failed to fetch dynamically imported module".
 * Listen to Vite's preloadError and do a full page reload once (the reload fetches the
 * no-cache new index -> new chunk names). A 10s dedupe prevents a reload storm: if the
 * same chunk still can't be fetched after reload (real 404 / offline), don't reload again within 10s.
 */
export function installPreloadErrorReload() {
  window.addEventListener('vite:preloadError', () => {
    const KEY = 'vite-preload-reload-ts';
    const last = Number(sessionStorage.getItem(KEY) || 0);
    if (Date.now() - last < 10000) return;
    sessionStorage.setItem(KEY, String(Date.now()));
    window.location.reload();
  });
}
