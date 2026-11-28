import type React from 'react';
import { message } from 'antd';
import { t } from '../i18n';
import { uploadErrorMessage } from '../api';
import { ApiResponseError } from '../utils/apiError';
import { uploadFileToOSS } from '../utils/fileParser';
import { readComposer, useComposerStore } from '../stores/composerStore';
import { useChatStore } from '../stores/chatStore';

export function useComposerFiles(effectiveApiUrl: string) {
  function handleFileSelect(
    e: React.ChangeEvent<HTMLInputElement>,
    fileInputRef: React.RefObject<HTMLInputElement | null>,
    draftKey = useComposerStore.getState().activeKey,
  ) {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    const picked = Array.from(files);
    if (fileInputRef.current) fileInputRef.current.value = '';
    attachFiles(picked, draftKey);
  }

  function attachFiles(picked: readonly File[], draftKey = useComposerStore.getState().activeKey) {
    // 不在前端写死大小上限：单文件上限来自后台权限配置（角色 / 团队 / 个人的「我的空间」上限），
    // 由后端校验；超限时把后端给出的原因（含配置的 MB 数）显示在该文件上，而不是笼统的「上传失败」。
    const newFiles = [...picked];
    if (newFiles.length === 0) return;

    const owner = readComposer(draftKey);
    const { setUploadedFiles, uploadedFiles } = owner;
    setUploadedFiles([...uploadedFiles, ...newFiles]);

    for (const file of newFiles) startUpload(file, draftKey);
  }

  function startUpload(file: File, draftKey: string) {
    const owner = readComposer(draftKey);
    if (!owner.uploadedFiles.includes(file) || owner.uploadingFiles.has(file)) return;
    const epoch = useComposerStore.getState().epoch;
    const chat = useChatStore.getState();
    const chatId = chat.currentChatId;
    const projectId = chat.store.chats[chatId]?.projectId;
    owner.addUploadingFile(file);
    // The promise identity also guards removal/retry and draft transfer while I/O is pending.
    const current = () => useComposerStore.getState().epoch === epoch && owner.uploads.get(file) === pending;
    const pending = uploadFileToOSS(file, effectiveApiUrl, chatId, projectId)
      .then(result => {
        if (current()) {
          if (result.file_id) owner.setUploadedArtifact(file, result);
          else owner.setUploadError(file, t('上传失败，请重试'));
        }
        return result;
      })
      .catch((error: unknown) => {
        if (current()) {
          const tooLarge = error instanceof ApiResponseError && error.status === 413;
          const reason = tooLarge ? uploadErrorMessage(413, error.payload) : t('上传失败，请重试');
          owner.setUploadError(file, reason);
          if (tooLarge) message.error(t('「{name}」上传失败：{reason}', { name: file.name, reason }));
        }
        return { file_id: '', download_url: '' };
      })
      .finally(() => { if (current()) owner.removeUploadingFile(file); });
    owner.uploads.set(file, pending);
  }

  function retryFile(file: File, draftKey = useComposerStore.getState().activeKey) {
    startUpload(file, draftKey);
  }

  function removeFile(index: number, draftKey = useComposerStore.getState().activeKey) {
    const owner = readComposer(draftKey);
    const { uploadedFiles, setUploadedFiles, removeUploadingFile } = owner;
    const removedFile = uploadedFiles[index];
    if (removedFile) {
      owner.uploads.delete(removedFile);
      removeUploadingFile(removedFile);
    }
    setUploadedFiles(uploadedFiles.filter((_, i) => i !== index));
  }

  return { handleFileSelect, attachFiles, removeFile, retryFile };
}
