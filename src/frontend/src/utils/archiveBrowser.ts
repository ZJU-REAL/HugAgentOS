import { artifactUrl, type ArtifactLocation } from './artifactAccess';
import { t } from '../i18n';

export interface ArchiveEntry {
  path: string;
  size: number;
  is_dir: boolean;
  readable: boolean;
  reason: string | null;
}

export interface ArchiveListing {
  format: string;
  entries: ArchiveEntry[];
  blocked_count: number;
}

const ARCHIVE_EXTENSIONS = new Set(['zip', 'tar', 'tgz', 'tbz', 'tbz2', 'txz', 'gz', 'bz2', 'xz', 'rar', '7z']);
const ARCHIVE_MIMES = new Set([
  'application/zip', 'application/x-zip-compressed', 'application/x-tar',
  'application/gzip', 'application/x-gzip', 'application/x-bzip2',
  'application/x-xz', 'application/x-7z-compressed', 'application/vnd.rar',
  'application/x-rar-compressed',
]);

export function isArchiveFile(file: { name: string; mime_type?: string }): boolean {
  return ARCHIVE_EXTENSIONS.has(file.name.split('.').pop()?.toLowerCase() || '')
    || ARCHIVE_MIMES.has(file.mime_type?.toLowerCase() || '');
}

export function archiveUrl(file: ArtifactLocation, memberPath?: string): string {
  const raw = artifactUrl(file);
  if (!raw) return '';
  const url = new URL(raw, 'http://artifact.invalid');
  const space = /^(?:\/api)?\/v1\/myspace\/content$/.test(url.pathname);
  if (!space && !/^(?:\/api)?\/files\/[^/]+$/.test(url.pathname)) return '';
  url.pathname += '/archive' + (memberPath === undefined ? '' : '/member');
  if (memberPath !== undefined) url.searchParams.set(space ? 'member' : 'path', memberPath);
  return /^https?:\/\//i.test(raw) ? url.toString() : url.pathname + url.search;
}

export function archiveError(code: unknown): string {
  switch (code) {
    case 'archive_format_unsupported': return t('暂不支持此压缩格式，请下载后解压。支持 ZIP、TAR、GZ、BZ2、XZ');
    case 'archive_invalid': return t('压缩包已损坏或格式不正确');
    case 'archive_too_large': return t('压缩包超过 100 MB，请下载后解压');
    case 'archive_decoder_memory': return t('压缩包解码所需内存超过上限，请下载后解压');
    case 'archive_directory_too_large': return t('压缩包目录信息超过上限，请下载后解压');
    case 'archive_too_many_entries': return t('压缩包超过 5000 个条目，请下载后解压');
    case 'archive_scan_too_large': return t('压缩包展开后超过浏览上限，请下载后解压');
    case 'archive_member_too_large': return t('包内文件超过 30 MB，请下载原压缩包后解压');
    case 'archive_encrypted': return t('加密文件，请下载原压缩包后使用密码解压');
    case 'archive_special_file': return t('链接或特殊文件不支持读取');
    case 'archive_ambiguous_path': return t('包内路径重复或冲突，不支持读取');
    case 'archive_member_missing': return t('包内文件不存在，请重新打开压缩包');
    case 'archive_compression_unsupported': return t('此压缩算法暂不支持，请下载后解压');
    default: return t('无法读取压缩包，请重试或下载后解压');
  }
}

/** Show direct children, including implicit parent folders absent from ZIP/TAR headers. */
export function archiveChildren(entries: ArchiveEntry[], folder: string): ArchiveEntry[] {
  const prefix = folder ? folder + '/' : '';
  const children = new Map<string, ArchiveEntry>();
  for (const entry of entries) {
    if (!entry.path.startsWith(prefix) || entry.path === folder) continue;
    const rest = entry.path.slice(prefix.length);
    const slash = rest.indexOf('/');
    if (slash >= 0) {
      const path = prefix + rest.slice(0, slash);
      if (!children.has(path)) children.set(path, {
        path, size: 0, is_dir: true, readable: false, reason: null,
      });
    } else {
      children.set(entry.path, entry);
    }
  }
  return [...children.values()].sort((a, b) =>
    Number(b.is_dir) - Number(a.is_dir) || a.path.localeCompare(b.path));
}
