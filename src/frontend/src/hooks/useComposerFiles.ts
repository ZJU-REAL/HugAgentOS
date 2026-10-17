import type React from 'react';
import { message } from 'antd';
import { t } from '../i18n';
import { UPLOAD_MAX_BYTES, UPLOAD_MAX_MB } from '../api';
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
    // 超限的文件当场拦下并说明上限：以前既不校验、上传失败也被 catch 吞掉，
    // 挑一个 90MB 的文件会「安静地」挂在输入框上，用户完全不知道它没传上去（问题 42）。
    // 上限与 nginx 的 client_max_body_size 同一个来源（VITE_UPLOAD_MAX_MB）。
    const oversized = picked.filter((f) => f.size > UPLOAD_MAX_BYTES);
    const newFiles = picked.filter((f) => f.size <= UPLOAD_MAX_BYTES);
    if (oversized.length > 0) {
      message.error(
        oversized.length === 1
          ? t('「{name}」超过 {n} MB，无法上传', { name: oversized[0].name, n: UPLOAD_MAX_MB })
          : t('{k} 个文件超过 {n} MB，已跳过', { k: oversized.length, n: UPLOAD_MAX_MB }),
      );
    }
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
      .catch(() => {
        if (current()) owner.setUploadError(file, t('上传失败，请重试'));
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
