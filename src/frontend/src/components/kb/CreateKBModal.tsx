import { useRef, useState } from 'react';
import { Modal, Input, Typography, message } from 'antd';
import { useKbStore } from '../../stores';
import { createKBSpace } from '../../api';
import { t } from '../../i18n';

interface CreateKBModalProps {
  onCreated?: () => void;
}

export default function CreateKBModal({ onCreated }: CreateKBModalProps) {
  const {
    createKBModalOpen,
    createKBName,
    createKBDesc,
    createKBLoading,
    closeCreateKBModal,
    setCreateKBName,
    setCreateKBDesc,
    setCreateKBLoading,
  } = useKbStore();

  // Empty-name submit: status=error persists until the user edits it, shake plays once
  const [nameError, setNameError] = useState(false);
  const [nameShaking, setNameShaking] = useState(false);
  const shakeTimerRef = useRef<number | undefined>(undefined);
  const triggerNameInvalid = () => {
    setNameError(true);
    setNameShaking(true);
    window.clearTimeout(shakeTimerRef.current);
    shakeTimerRef.current = window.setTimeout(() => setNameShaking(false), 400);
  };

  return (
    <Modal
      title={t('新增私有知识库')}
      open={createKBModalOpen}
      onCancel={closeCreateKBModal}
      confirmLoading={createKBLoading}
      okText={t('创建')}
      cancelText={t('取消')}
      onOk={async () => {
        if (!createKBName.trim()) { triggerNameInvalid(); message.warning(t('请输入知识库名称')); return; }
        setCreateKBLoading(true);
        try {
          await createKBSpace(createKBName.trim(), createKBDesc.trim() || undefined);
          message.success(t('知识库创建成功'));
          closeCreateKBModal();
          onCreated?.();
        } catch (err: any) {
          message.error(err.message || t('创建失败'));
        } finally {
          setCreateKBLoading(false);
        }
      }}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, paddingTop: 8 }}>
        <div>
          <div style={{ marginBottom: 4, fontWeight: 600, fontSize: 13 }}>{t('名称')} <span style={{ color: 'red' }}>*</span></div>
          <Input
            placeholder={t('知识库名称')}
            value={createKBName}
            status={nameError ? 'error' : undefined}
            className={nameShaking ? 'jx-anim-shake' : undefined}
            onChange={(e) => {
              setCreateKBName(e.target.value);
              if (nameError) setNameError(false);
            }}
            maxLength={255}
          />
        </div>
        <div>
          <div style={{ marginBottom: 4, fontWeight: 600, fontSize: 13 }}>{t('描述')}</div>
          <Input.TextArea
            placeholder={t('知识库描述（可选）')}
            value={createKBDesc}
            onChange={(e) => setCreateKBDesc(e.target.value)}
            rows={3}
            maxLength={150}
            showCount
          />
        </div>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {t('分块方法可在上传文档时逐文件选择')}
        </Typography.Text>
      </div>
    </Modal>
  );
}
