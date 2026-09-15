import { uploadFile, type UploadedFile } from '../api';
import { t } from '../i18n';

export type UploadedAttachment = Pick<UploadedFile, 'file_id' | 'download_url' | 'origin'>;

export async function uploadFileToOSS(
  file: File, apiUrl: string, chatId: string, projectId?: string,
): Promise<UploadedAttachment> {
  if (!apiUrl) return { file_id: '', download_url: '' };
  try {
    return await uploadFile(file, chatId, undefined, { apiUrl, projectId });
  } catch { /* The composer reports the failed upload and preserves the draft. */ }
  return { file_id: '', download_url: '' };
}

export function normalizeArtifactOutput(raw: unknown): Record<string, unknown> | null {
  if (!raw || typeof raw !== 'object') return null;
  const artifact = raw as Record<string, unknown>;
  const fileId = typeof artifact.file_id === 'string' ? artifact.file_id.trim() : '';
  const url = typeof artifact.url === 'string'
    ? artifact.url.trim()
    : typeof artifact.download_url === 'string'
    ? artifact.download_url.trim()
    : '';
  if (!fileId || !url) return null;

  const typeName = typeof artifact.type === 'string' && artifact.type.trim()
    ? artifact.type.trim()
    : t('附件');
  const output: Record<string, unknown> = {
    ...(artifact.origin === 'local' || artifact.origin === 'cloud' ? { origin: artifact.origin } : {}),
    ok: true,
    file_id: fileId,
    url,
    name:
      typeof artifact.name === 'string' && artifact.name.trim()
        ? artifact.name.trim()
        : `${typeName}_${fileId}`,
  };
  if (typeof artifact.mime_type === 'string' && artifact.mime_type.trim()) {
    output.mime_type = artifact.mime_type.trim();
  }
  if (typeof artifact.size === 'number' && Number.isFinite(artifact.size)) {
    output.size = artifact.size;
  }
  return output;
}

export function extractArtifactOutputs(raw: unknown): Record<string, unknown>[] {
  const results: Record<string, unknown>[] = [];
  const seen = new Set<string>();

  const pushOutput = (candidate: unknown, origin?: 'local' | 'cloud') => {
    const output = normalizeArtifactOutput(candidate);
    if (!output) return;
    if (!output.origin && origin) output.origin = origin;
    const fileId = String(output.file_id);
    if (seen.has(fileId)) return;
    seen.add(fileId);
    results.push(output);
  };

  const visit = (candidate: unknown, inheritedOrigin?: 'local' | 'cloud') => {
    if (!candidate) return;
    if (Array.isArray(candidate)) {
      for (const item of candidate) visit(item, inheritedOrigin);
      return;
    }
    if (typeof candidate !== 'object') return;

    const record = candidate as Record<string, unknown>;
    const metadata = record.metadata && typeof record.metadata === 'object'
      ? record.metadata as Record<string, unknown> : {};
    const explicitOrigin = record.origin || metadata.origin;
    const origin = explicitOrigin === 'local' || explicitOrigin === 'cloud' ? explicitOrigin : inheritedOrigin;
    pushOutput(candidate, origin);
    if (Array.isArray(record.artifacts)) visit(record.artifacts, origin);
    if (Array.isArray(record.files)) visit(record.files, origin);
    if (record.result && record.result !== candidate) visit(record.result, origin);
  };

  visit(raw);
  return results;
}

export function attachArtifactsToToolCalls(
  baseToolCalls: import('../types').ChatMessage['toolCalls'],
  artifacts: unknown[],
  timestamp: number
): import('../types').ChatMessage['toolCalls'] {
  const merged = Array.isArray(baseToolCalls) ? [...baseToolCalls] : [];
  const existingFileIds = new Set<string>();

  for (const tool of merged) {
    if (!tool || typeof tool !== 'object') continue;
    const output = tool.output;
    if (!output || typeof output !== 'object') continue;
    const fileId = (output as Record<string, unknown>).file_id;
    if (typeof fileId === 'string' && fileId.trim()) {
      existingFileIds.add(fileId.trim());
    }
  }

  for (const artifact of artifacts) {
    const output = normalizeArtifactOutput(artifact);
    if (!output) continue;
    const fileId = String(output.file_id);
    if (existingFileIds.has(fileId)) continue;
    existingFileIds.add(fileId);
    merged.push({
      id: `artifact_${fileId}`,
      name: t('附件'),
      output,
      status: 'success',
      timestamp,
    });
  }

  return merged.length > 0 ? merged : undefined;
}
