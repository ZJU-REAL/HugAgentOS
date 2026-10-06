import type React from 'react';
import { message } from 'antd';
import { t } from '../i18n';
import { UPLOAD_MAX_BYTES, UPLOAD_MAX_MB } from '../api';
import { uploadFileToOSS } from '../utils/fileParser';
import { readComposer, useComposerStore, projectDraftKey } from '../stores/composerStore';
import { useChatStore } from '../stores/chatStore';
import { useProjectStore } from '../stores/projectStore';

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

    const curApiUrl = effectiveApiUrl ?? '';
    const project = useProjectStore.getState().currentProject;
    const isProjectDraft = !!project && owner.key === projectDraftKey(project.project_id);
    const curChatId = isProjectDraft ? undefined : useChatStore.getState().currentChatId;
    const curProjectId = isProjectDraft ? project.project_id
      : curChatId ? useChatStore.getState().store.chats[curChatId]?.projectId : undefined;

    for (const file of newFiles) {
      const { addUploadingFile, removeUploadingFile } = owner;
      addUploadingFile(file);
      const promise = uploadFileToOSS(file, curApiUrl, curChatId, curProjectId)
        .then((res) => {
          // uploadFileToOSS 失败时返回空 file_id 而不是抛错。以前这里不看返回值，
          // 附件就静静地停在输入框上、实际根本没传上去，发送时也不会带上。
          if (!res.file_id) {
            message.error(t('「{name}」上传失败，请重试', { name: file.name }));
          } else {
            owner.setUploadedArtifact(file, res);
          }
          return res;
        })
        .catch(() => {
          message.error(t('「{name}」上传失败，请重试', { name: file.name }));
          return { file_id: '', download_url: '' };
        })
        .finally(() => { removeUploadingFile(file); });
      owner.uploads.set(file, promise);
    }
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

  return { handleFileSelect, removeFile };
}
