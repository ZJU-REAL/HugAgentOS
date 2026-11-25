import { useEffect, useState } from 'react';
import { authFetch, getApiUrl } from '../api';
import { isPublicChat } from '../publicChatTransport';
import { readLimitedArrayBuffer } from '../utils/filePreviewSafety';

// Memory guard for buffering public-chat media into a blob URL (a preview bound, not an upload cap).
const PUBLIC_MEDIA_MAX_BYTES = 200 * 1024 * 1024;

/** Media elements cannot carry the public visitor headers; use a bounded blob URL. */
export function usePublicMediaUrl(src: string): { url: string; error?: string } {
  const needsCredential = !!src && !src.startsWith('blob:') && isPublicChat() && new URL(src, window.location.href).origin === new URL(getApiUrl(), window.location.href).origin;
  const [result, setResult] = useState<{ source: string; url: string; error?: string }>();
  useEffect(() => {
    if (!needsCredential) return;
    const controller = new AbortController();
    let blobUrl: string | undefined;
    void (async () => {
      const response = await authFetch(src, { signal: controller.signal });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const bytes = await readLimitedArrayBuffer(response, PUBLIC_MEDIA_MAX_BYTES);
      if (controller.signal.aborted) return;
      blobUrl = URL.createObjectURL(new Blob([bytes], { type: response.headers.get('content-type') || 'application/octet-stream' }));
      setResult({ source: src, url: blobUrl });
    })().catch(error => {
      if (!controller.signal.aborted) setResult({ source: src, url: '', error: String(error) });
    });
    return () => { controller.abort(); if (blobUrl) URL.revokeObjectURL(blobUrl); };
  }, [src, needsCredential]);
  return needsCredential ? (result?.source === src ? result : { url: '' }) : { url: src };
}
