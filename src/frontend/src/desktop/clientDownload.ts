export type ClientDownloadTarget =
  | 'darwin-aarch64' | 'darwin-x86_64'
  | 'windows-x86_64' | 'windows-aarch64'
  | 'linux-x86_64' | 'linux-aarch64';

/** Pick the release for the browser's operating system and known CPU architecture. */
export function clientDownloadTarget(userAgent: string, platform: string, touchPoints = 0): ClientDownloadTarget | null {
  if (/iphone|ipad|ipod|android|mobile/i.test(userAgent) || (platform === 'MacIntel' && touchPoints > 1)) return null;
  const os = `${platform} ${userAgent}`.toLowerCase();
  const arm = /aarch64|arm64|windows arm/.test(os);
  if (/macintosh|mac os|macintel|macarm/.test(os)) return /x86_64/.test(os) && !arm ? 'darwin-x86_64' : 'darwin-aarch64';
  if (/windows|win32|win64/.test(os)) return arm ? 'windows-aarch64' : 'windows-x86_64';
  if (/linux|x11/.test(os)) return arm ? 'linux-aarch64' : 'linux-x86_64';
  return null;
}

/** The updater manifest is the only source of published platform versions. */
export async function latestClientDownloadUrl(
  target: ClientDownloadTarget,
  apiBase: string,
  fetcher: typeof fetch = fetch,
): Promise<string | null> {
  const [os, arch] = target.split('-');
  const base = new URL(apiBase.replace(/\/+$/, '') + '/', window.location.href);
  const manifestUrl = new URL(`v1/desktop/latest.json?target=${os}&arch=${arch}`, base);
  const response = await fetcher(manifestUrl.toString(), { cache: 'no-store' });
  if (response.status === 204) return null;
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const manifest = await response.json();
  const rawUrl = manifest?.platforms?.[target]?.url;
  if (typeof rawUrl !== 'string' || !rawUrl) return null;
  const downloadUrl = new URL(rawUrl, base);
  if (downloadUrl.origin !== base.origin ||
      !downloadUrl.pathname.startsWith(base.pathname + 'v1/desktop/download/')) {
    throw new Error('Invalid desktop download URL');
  }
  return downloadUrl.toString();
}
