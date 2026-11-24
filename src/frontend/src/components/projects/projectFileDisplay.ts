import type { ProjectFileItem } from '../../types';

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** Display name for a file inside the project: strip the folder_path prefix, keep only the file name itself. */
export function leafName(item: ProjectFileItem): string {
  const i = item.name.lastIndexOf('/');
  return i === -1 ? item.name : item.name.slice(i + 1);
}
