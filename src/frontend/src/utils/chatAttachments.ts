import { isHybridDual, chatUploadTarget, uploadFile, authFetch, LOCAL_TARGET_HEADER, UPLOAD_MAX_BYTES } from '../api';
import { t } from '../i18n';
import type { UploadedAttachment } from './fileParser';
import type { ImportedSpaceFile } from '../stores/fileStore';

export interface ChatAttachment extends UploadedAttachment {
  name: string;
  mime_type: string;
}

/** Bound downloads even when a generated resource has no Content-Length header. */
async function readReferencedFile(response: Response, name: string): Promise<Blob> {
  const tooLarge = () => new Error(t('文件过大，无法添加：{name}', { name }));
  if (Number(response.headers.get('content-length')) > UPLOAD_MAX_BYTES) {
    await response.body?.cancel();
    throw tooLarge();
  }
  if (!response.body) throw new Error(t('无法读取引用文件：{name}', { name }));
  const reader = response.body.getReader();
  const chunks: BlobPart[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > UPLOAD_MAX_BYTES) {
        await reader.cancel();
        throw tooLarge();
      }
      chunks.push(new Uint8Array(value));
    }
  } finally {
    reader.releaseLock();
  }
  return new Blob(chunks);
}

/** Assemble attachments before committing a user message in either chat or plan mode. */
export async function prepareChatAttachments(
  files: File[], uploads: Map<File, Promise<UploadedAttachment>>,
  imported: ImportedSpaceFile[], apiUrl: string, chatId: string, projectId?: string,
): Promise<ChatAttachment[]> {
  const target = chatUploadTarget(chatId, projectId);
  const attachments: ChatAttachment[] = [];
  for (const file of files) {
    let result = await uploads.get(file)?.catch(() => undefined);
    if (!result?.file_id || result.origin !== target) {
      const pending = uploadFile(file, chatId, undefined, { apiUrl, target });
      uploads.set(file, pending);
      result = await pending;
    }
    if (!result.file_id) throw new Error(t('文件上传失败，请移除后重试：{names}', { names: file.name }));
    attachments.push({ ...result, name: file.name, mime_type: file.type });
  }
  for (const file of imported) {
    // The space picker lists cloud resources in dual mode. Legacy picks have no origin.
    const source = file.origin ?? (isHybridDual() ? 'cloud' : undefined);
    if (!target || source === target) {
      attachments.push({ ...file, origin: source });
      continue;
    }
    const response = await authFetch(`${apiUrl}/files/${encodeURIComponent(file.file_id)}`, {
      headers: source ? { [LOCAL_TARGET_HEADER]: source } : {},
    });
    if (!response.ok) throw new Error(t('无法读取引用文件：{name}', { name: file.name }));
    const blob = await readReferencedFile(response, file.name);
    const uploaded = await uploadFile(new File([blob], file.name, { type: file.mime_type }), chatId, undefined, { apiUrl, target });
    if (!uploaded.file_id) throw new Error(t('文件上传失败，请移除后重试：{names}', { names: file.name }));
    attachments.push({ ...uploaded, name: file.name, mime_type: file.mime_type });
  }
  if (chatUploadTarget(chatId, projectId) !== target) {
    throw new Error(t('运行位置已变化，请重新发送。'));
  }
  return attachments;
}
