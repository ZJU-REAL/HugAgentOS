import assert from 'node:assert/strict';

import { extractClipboardImageFiles } from '../src/utils/clipboardFiles';

const png = { name: 'screenshot.png', type: 'image/png' } as File;
const webp = { name: 'diagram.webp', type: 'image/webp' } as File;
const pdf = { name: 'notes.pdf', type: 'application/pdf' } as File;

function clipboardData(
  items: Array<{ kind: string; type: string; getAsFile: () => File | null }>,
  files: File[] = [],
): DataTransfer {
  return { items, files } as unknown as DataTransfer;
}

{
  const images = extractClipboardImageFiles(clipboardData([
    { kind: 'string', type: 'text/plain', getAsFile: () => null },
    { kind: 'file', type: 'application/pdf', getAsFile: () => pdf },
    { kind: 'file', type: 'image/png', getAsFile: () => png },
    { kind: 'file', type: 'image/webp', getAsFile: () => webp },
  ]));

  assert.deepEqual(images, [png, webp]);
}

{
  // Some clipboard implementations leave DataTransferItem.type empty even though
  // the returned File carries the correct MIME type.
  const images = extractClipboardImageFiles(clipboardData([
    { kind: 'file', type: '', getAsFile: () => png },
  ]));

  assert.deepEqual(images, [png]);
}

{
  // Fall back to clipboardData.files when the browser does not expose usable items.
  const images = extractClipboardImageFiles(clipboardData([], [pdf, png]));

  assert.deepEqual(images, [png]);
}

{
  assert.deepEqual(extractClipboardImageFiles(null), []);
}

console.log('clipboard image paste tests passed');
