export type PreviewKind =
  | 'docx'
  | 'xlsx'
  | 'pdf'
  | 'ppt'
  | 'pptx'
  | 'image'
  | 'markdown'
  | 'text'
  | 'html'
  | 'json'
  | 'csv'
  | 'video'
  | 'audio'
  | 'unknown';

const MB = 1024 * 1024;

/**
 * Per-format limits for browser-side previews.
 *
 * These are deliberately lower than upload limits: previewing expands
 * compressed Office files into DOM/canvas objects and can consume many times
 * the original file size. Audio/video are excluded because the native player
 * can stream them without materialising the complete file in JavaScript.
 */
const PREVIEW_LIMIT_BYTES: Partial<Record<PreviewKind, number>> = {
  docx: 20 * MB,
  xlsx: 8 * MB,
  pdf: 30 * MB,
  ppt: 20 * MB,
  pptx: 20 * MB,
  image: 25 * MB,
  markdown: 2 * MB,
  text: 2 * MB,
  html: 2 * MB,
  json: 2 * MB,
  csv: 2 * MB,
};

export class PreviewFileTooLargeError extends Error {
  readonly limitBytes: number;
  readonly actualBytes?: number;

  constructor(limitBytes: number, actualBytes?: number) {
    super(`Preview exceeds ${limitBytes} bytes`);
    this.name = 'PreviewFileTooLargeError';
    this.limitBytes = limitBytes;
    this.actualBytes = actualBytes;
  }
}

export function getPreviewLimitBytes(kind: PreviewKind): number | null {
  return PREVIEW_LIMIT_BYTES[kind] ?? null;
}

export function exceedsPreviewLimit(size: number | undefined, limitBytes: number | null): boolean {
  return limitBytes !== null && typeof size === 'number' && size > limitBytes;
}

export function formatPreviewBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < MB) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / MB).toFixed(1)} MB`;
}

function contentLengthOf(response: Response): number | undefined {
  const raw = response.headers.get('content-length');
  if (!raw) return undefined;
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined;
}

/**
 * Consume a response with a hard byte ceiling, including when Content-Length
 * is absent or inaccurate. Cancelling the reader prevents an obsolete or
 * oversized preview from continuing to download in the background.
 */
export async function readLimitedBlob(response: Response, limitBytes: number): Promise<Blob> {
  const declaredBytes = contentLengthOf(response);
  if (declaredBytes !== undefined && declaredBytes > limitBytes) {
    await response.body?.cancel();
    throw new PreviewFileTooLargeError(limitBytes, declaredBytes);
  }

  if (!response.body) {
    const blob = await response.blob();
    if (blob.size > limitBytes) {
      throw new PreviewFileTooLargeError(limitBytes, blob.size);
    }
    return blob;
  }

  const reader = response.body.getReader();
  const chunks: ArrayBuffer[] = [];
  let totalBytes = 0;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      totalBytes += value.byteLength;
      if (totalBytes > limitBytes) {
        await reader.cancel();
        throw new PreviewFileTooLargeError(limitBytes, totalBytes);
      }

      // Copy into an ArrayBuffer-backed view so BlobPart never receives a
      // SharedArrayBuffer-capable generic from the DOM stream typings.
      const copy = new Uint8Array(value.byteLength);
      copy.set(value);
      chunks.push(copy.buffer);
    }
  } finally {
    reader.releaseLock();
  }

  return new Blob(chunks, {
    type: response.headers.get('content-type') || 'application/octet-stream',
  });
}

export async function readLimitedText(response: Response, limitBytes: number): Promise<string> {
  return (await readLimitedBlob(response, limitBytes)).text();
}

export async function readLimitedArrayBuffer(response: Response, limitBytes: number): Promise<ArrayBuffer> {
  return (await readLimitedBlob(response, limitBytes)).arrayBuffer();
}
