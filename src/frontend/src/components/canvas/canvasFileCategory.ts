import type { CanvasArtifact } from '../../stores/canvasStore';

import { isArchiveFile } from '../../utils/archiveBrowser';

function getFileExt(name: string): string {
  return (name.split('.').pop() || '').toLowerCase();
}

export function getFileCategory(artifact: CanvasArtifact): 'docx' | 'xlsx' | 'pdf' | 'ppt' | 'image' | 'markdown' | 'text' | 'html' | 'archive' | 'unknown' {
  if (isArchiveFile(artifact)) return 'archive';
  const ext = getFileExt(artifact.name);
  const mime = artifact.mime_type || '';
  if (ext === 'docx' || ext === 'doc' || mime.includes('wordprocessingml')) return 'docx';
  if (ext === 'xlsx' || ext === 'xls' || mime.includes('spreadsheetml')) return 'xlsx';
  if (ext === 'pdf' || mime === 'application/pdf') return 'pdf';
  if (ext === 'pptx' || ext === 'ppt' || mime.includes('presentationml') || mime.includes('powerpoint')) return 'ppt';
  if (mime.startsWith('image/') || ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'avif', 'ico'].includes(ext)) return 'image';
  if (ext === 'html' || ext === 'htm' || mime === 'text/html') return 'html';
  if (['md', 'markdown', 'mdx'].includes(ext) || mime === 'text/markdown') return 'markdown';
  if (['txt', 'log', 'csv', 'tsv', 'json', 'json5', 'jsonl', 'ndjson', 'xml', 'yaml', 'yml', 'ini', 'conf', 'env', 'toml', 'lock', 'py', 'js', 'ts', 'tsx', 'jsx', 'mjs', 'cjs', 'css', 'scss', 'sass', 'less', 'sh', 'bash', 'zsh', 'fish', 'ps1', 'bat', 'cmd', 'go', 'rs', 'java', 'kt', 'swift', 'c', 'cpp', 'cc', 'h', 'hpp', 'rb', 'php', 'r', 'sql', 'graphql', 'gql', 'proto', 'vue', 'svelte', 'astro'].includes(ext) || mime.startsWith('text/')) return 'text';
  return 'unknown';
}
