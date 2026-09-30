import type { ProjectFileItem } from '../../types';
import { t } from '../../i18n';

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** Use the file extension as a short type label (do not show the verbose mime). */
export function shortType(item: ProjectFileItem): string {
  const name = item.name || '';
  const idx = name.lastIndexOf('.');
  if (idx > 0 && idx < name.length - 1) {
    return name.slice(idx + 1).toUpperCase();
  }
  const mime = item.mime_type || '';
  if (mime.startsWith('image/')) return mime.slice(6).toUpperCase();
  if (mime === 'application/pdf') return 'PDF';
  return t('文件');
}

/** Display name for a file inside the project: strip the folder_path prefix, keep only the file name itself. */
export function leafName(item: ProjectFileItem): string {
  const i = item.name.lastIndexOf('/');
  return i === -1 ? item.name : item.name.slice(i + 1);
}
