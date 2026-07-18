import { useEffect, useState } from 'react';
import { Modal, Input, message } from 'antd';
import { useMySpaceStore } from '../../../stores/mySpaceStore';
import { t } from '../../../i18n';

interface Props {
  open: boolean;
  onClose: () => void;
  /** Created under the current selectedScope.folderId by default; can be overridden by explicitly specifying parentFolderId. */
  parentFolderId?: string | null;
}

export function CreatePersonalFolderModal({ open, onClose, parentFolderId }: Props) {
  const { selectedScope, createPersonalFolderAction } = useMySpaceStore();
  const [name, setName] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      setName('');
      setSubmitting(false);
    }
  }, [open]);

  const effectiveParent =
    parentFolderId !== undefined
      ? parentFolderId
      : selectedScope.kind === 'personal'
        ? selectedScope.folderId ?? null
        : null;

  const handleOk = async () => {
    const cleaned = name.trim();
    if (!cleaned) {
      message.warning(t('请输入文件夹名称'));
      return;
    }
    if (cleaned.length > 255) {
      message.warning(t('文件夹名称过长（≤255 字符）'));
      return;
    }
    if (cleaned.includes('/') || cleaned === '.' || cleaned === '..') {
      message.warning(t('文件夹名称非法'));
      return;
    }
    setSubmitting(true);
    try {
      await createPersonalFolderAction(cleaned, effectiveParent);
      message.success(t('文件夹已创建'));
      onClose();
    } catch (e: any) {
      message.error(e?.message || t('创建失败'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title={t('新建文件夹')}
      open={open}
      onCancel={onClose}
      onOk={handleOk}
      confirmLoading={submitting}
      okText={t('创建')}
      cancelText={t('取消')}
      destroyOnClose
    >
      <Input
        autoFocus
        placeholder={t('请输入文件夹名称')}
        value={name}
        maxLength={255}
        onChange={(e) => setName(e.target.value)}
        onPressEnter={() => void handleOk()}
      />
    </Modal>
  );
}
