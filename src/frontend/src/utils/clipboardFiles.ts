function isImageMime(type: string | undefined): boolean {
  return type?.trim().toLowerCase().startsWith('image/') ?? false;
}

/**
 * Read image files from a paste event without letting the contenteditable embed
 * base64 image markup. `items` is the primary path; `files` covers browsers that
 * expose clipboard files without usable DataTransferItem entries.
 */
export function extractClipboardImageFiles(
  clipboardData: Pick<DataTransfer, 'items' | 'files'> | null | undefined,
): File[] {
  if (!clipboardData) return [];

  const itemImages: File[] = [];
  for (let index = 0; index < clipboardData.items.length; index += 1) {
    const item = clipboardData.items[index];
    if (!item || item.kind !== 'file') continue;

    const file = item.getAsFile();
    if (file && (isImageMime(item.type) || isImageMime(file.type))) {
      itemImages.push(file);
    }
  }
  if (itemImages.length > 0) return itemImages;

  return Array.from(clipboardData.files).filter((file) => isImageMime(file.type));
}
