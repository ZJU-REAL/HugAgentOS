/** Saves an in-memory blob as a local file: ObjectURL → simulated `<a download>` click → release. */
export function saveBlob(blob: Blob, filename: string): void {
  const blobUrl = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = blobUrl;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(blobUrl);
}

/**
 * Downloads a URL to a local file with Bearer authentication.
 *
 * Used for exports such as CSV that require an Authorization header and cannot be triggered by a direct `<a download>` link:
 * fetch the blob → ObjectURL → simulate click → release. Throws on failure; the caller shows its own prompt.
 */
export async function downloadWithAuth(url: string, filename: string, token: string): Promise<void> {
  const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  saveBlob(await res.blob(), filename);
}
