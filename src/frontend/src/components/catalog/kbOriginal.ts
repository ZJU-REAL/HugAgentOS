import { getApiUrl } from '../../api';
import { getFilePreviewKind, type FilePreviewKind } from '../../utils/filePreviewKind';

/** 能按原版式预览的类型；其余格式只提供「下载原文」，正文仍看解析文本。 */
const PREVIEWABLE_KINDS = ['pdf', 'docx', 'image', 'text', 'markdown', 'csv'] as const satisfies readonly FilePreviewKind[];
export type KbOriginalKind = typeof PREVIEWABLE_KINDS[number];

export function kbOriginalKindOf(filename: string): KbOriginalKind | null {
  const kind = getFilePreviewKind({ name: filename });
  return (PREVIEWABLE_KINDS as readonly FilePreviewKind[]).includes(kind) ? kind as KbOriginalKind : null;
}

export function kbOriginalUrl(kbId: string, docId: string): string {
  return `${getApiUrl()}/v1/catalog/kb/${encodeURIComponent(kbId)}/documents/${encodeURIComponent(docId)}/original`;
}
