import { authFetch } from '../api';
import { t } from '../i18n';

export async function uploadFileToOSS(
  file: File, apiUrl: string, chatId: string
): Promise<{ file_id: string; download_url: string }> {
  if (!apiUrl) return { file_id: '', download_url: '' };
  try {
    const form = new FormData();
    form.append('file', file);
    form.append('chat_id', chatId);
    const resp = await authFetch(`${apiUrl}/v1/file/upload`, {
      method: 'POST',
      body: form,
    });
    if (resp.ok) {
      const data = await resp.json();
      return { file_id: data.file_id ?? '', download_url: data.download_url ?? '' };
    }
  } catch { /* 上传失败不阻断 */ }
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

  const pushOutput = (candidate: unknown) => {
    const output = normalizeArtifactOutput(candidate);
    if (!output) return;
    const fileId = String(output.file_id);
    if (seen.has(fileId)) return;
    seen.add(fileId);
    results.push(output);
  };

  const visit = (candidate: unknown) => {
    if (!candidate) return;
    if (Array.isArray(candidate)) {
      for (const item of candidate) visit(item);
      return;
    }
    if (typeof candidate !== 'object') return;

    pushOutput(candidate);

    const record = candidate as Record<string, unknown>;
    if (Array.isArray(record.artifacts)) visit(record.artifacts);
    if (Array.isArray(record.files)) visit(record.files);
    if (record.result && record.result !== candidate) visit(record.result);
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
