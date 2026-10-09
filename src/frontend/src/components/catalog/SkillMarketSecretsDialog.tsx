import { Modal, Typography, Form, Input } from 'antd';
import type { FormInstance } from 'antd';
import { t } from '../../i18n';
import type { MarketplaceSkill } from '../../types';

export function SkillMarketSecretsDialog({ secretSkill, secretForm, loading, onClose, onSubmit }: {
  secretSkill: MarketplaceSkill | null;
  secretForm: FormInstance;
  loading: boolean;
  onClose: () => void;
  onSubmit: () => Promise<void>;
}) {
  return (
      <Modal
        title={secretSkill ? t('配置「{name}」', { name: secretSkill.display_name }) : t('配置凭据')}
        open={!!secretSkill}
        onCancel={() => onClose()}
        onOk={() => void onSubmit()}
        okText={t('安装')}
        cancelText={t('取消')}
        confirmLoading={!!secretSkill && loading}
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          {t('该技能运行需要以下凭据。凭据仅保存在你安装的这份技能里，不会上传到技能市场。')}
        </Typography.Paragraph>
        <Form form={secretForm} layout="vertical">
          {secretSkill?.required_secrets.map((f) => (
            <Form.Item
              key={f.key}
              name={f.key}
              label={f.label || f.key}
              tooltip={f.help}
              extra={f.help}
              rules={f.required ? [{ required: true, message: t('请填写{label}', { label: f.label || f.key }) }] : []}
            >
              <Input.Password placeholder={f.placeholder || t('请输入 {label}', { label: f.label || f.key })} autoComplete="off" />
            </Form.Item>
          ))}
        </Form>
      </Modal>
  );
}
