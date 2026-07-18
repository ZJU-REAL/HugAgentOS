const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg']);

/** Maps file extensions to the corresponding icon paths under /icons/file/ */
export function getFileIconSrc(name: string): string {
  const ext = (name.split('.').pop() ?? '').toLowerCase();
  if (ext === 'pdf')                                    return '/icons/file/pdf.svg';
  if (ext === 'docx' || ext === 'doc' || ext === 'wps') return '/icons/file/word.svg';
  if (ext === 'xlsx' || ext === 'xls' || ext === 'csv') return '/icons/file/excel.svg';
  if (ext === 'pptx' || ext === 'ppt')                  return '/icons/file/ppt.svg';
  if (ext === 'txt')                                    return '/icons/file/txt.svg';
  if (ext === 'md')                                     return '/icons/file/md.svg';
  if (ext === 'json')                                   return '/icons/file/json.svg';
  if (IMAGE_EXTS.has(ext))                              return '/icons/file/image.svg';
  return '/icons/file/default.svg';
}

export function getFolderIconSrc(open = false): string {
  return open ? '/icons/file/folder-open.svg' : '/icons/file/folder.svg';
}
