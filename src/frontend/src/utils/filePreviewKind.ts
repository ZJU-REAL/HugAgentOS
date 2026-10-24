import { isArchiveFile } from './archiveBrowser';
import { isArchiveMember, type ArtifactLocation } from './artifactAccess';

/**
 * The single file-type classifier for every preview surface (canvas sidebar,
 * file preview pane, public chat) and for the per-format preview size limits.
 *
 * - `docx`: OOXML Word, parsed in the browser by docx-preview.
 * - `office`: everything else in the Office family that the browser cannot
 *   parse (legacy binary .doc/.ppt, PPTX, OpenDocument, RTF). The server
 *   converts it to PDF (`/preview?format=pdf`).
 * - `xlsx`: spreadsheets parsed in the browser (SheetJS / Univer).
 */
export type FilePreviewKind =
  | 'archive'
  | 'image'
  | 'video'
  | 'audio'
  | 'pdf'
  | 'docx'
  | 'office'
  | 'xlsx'
  | 'markdown'
  | 'html'
  | 'json'
  | 'csv'
  | 'text'
  | 'unknown';

export interface PreviewFile extends ArtifactLocation {
  name: string;
  mime_type?: string;
}

const EXTENSION_KINDS: Record<string, FilePreviewKind> = {};
function register(kind: FilePreviewKind, extensions: string[]) {
  for (const ext of extensions) EXTENSION_KINDS[ext] = kind;
}
register('image', ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'avif', 'ico']);
register('video', ['mp4', 'webm', 'mov', 'm4v', 'ogv']);
register('audio', ['mp3', 'wav', 'ogg', 'oga', 'opus', 'flac', 'm4a', 'aac']);
register('pdf', ['pdf']);
register('docx', ['docx']);
register('office', ['doc', 'rtf', 'odt', 'ppt', 'pptx', 'odp']);
register('xlsx', ['xlsx', 'xlsm', 'xls']);
register('markdown', ['md', 'markdown', 'mdx']);
register('html', ['html', 'htm']);
register('json', ['json', 'json5', 'jsonl', 'ndjson']);
register('csv', ['csv', 'tsv']);
register('text', [
  'txt', 'log', 'xml', 'yaml', 'yml', 'ini', 'conf', 'env', 'toml', 'lock',
  'py', 'js', 'ts', 'tsx', 'jsx', 'mjs', 'cjs', 'css', 'scss', 'sass', 'less',
  'sh', 'bash', 'zsh', 'fish', 'ps1', 'bat', 'cmd',
  'go', 'rs', 'java', 'kt', 'swift', 'c', 'cpp', 'cc', 'h', 'hpp',
  'rb', 'php', 'r', 'sql', 'graphql', 'gql', 'proto', 'vue', 'svelte', 'astro',
]);

export function fileExtension(name: string): string {
  const idx = name.lastIndexOf('.');
  return idx === -1 ? '' : name.slice(idx + 1).toLowerCase();
}

function kindFromMime(mime: string): FilePreviewKind {
  if (mime.includes('wordprocessingml')) return 'docx';
  if (mime.includes('msword') || mime.includes('presentationml') || mime.includes('powerpoint')
    || mime.includes('opendocument.text') || mime.includes('opendocument.presentation')) return 'office';
  if (mime.includes('spreadsheetml') || mime.includes('ms-excel')) return 'xlsx';
  if (mime === 'application/pdf') return 'pdf';
  if (mime.startsWith('image/')) return 'image';
  if (mime.startsWith('video/')) return 'video';
  if (mime.startsWith('audio/')) return 'audio';
  if (mime === 'text/markdown') return 'markdown';
  if (mime === 'text/html') return 'html';
  if (mime === 'application/json') return 'json';
  if (mime === 'text/csv') return 'csv';
  if (mime.startsWith('text/')) return 'text';
  return 'unknown';
}

/**
 * The extension wins over the MIME type: stored MIME types are often guessed
 * wrong (`.ts` → `video/mp2t`) or generic (`application/octet-stream`).
 */
export function getFilePreviewKind(file: PreviewFile): FilePreviewKind {
  const member = isArchiveMember(file);
  if (isArchiveFile(file)) return member ? 'unknown' : 'archive';
  // Archive members are served as text/plain when the server decides they are
  // safe to show as text (e.g. HTML/SVG are downgraded).
  const mime = (file.mime_type || '').toLowerCase();
  if (member && mime === 'text/plain') return 'text';
  return EXTENSION_KINDS[fileExtension(file.name)] ?? kindFromMime(mime);
}
