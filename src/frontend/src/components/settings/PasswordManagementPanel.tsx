import { useState } from 'react';
import { Alert, Button, Form, Input, Typography, message } from 'antd';
import { changeMyPassword } from '../../api';
import { t } from '../../i18n';
import { useAuthStore } from '../../stores';

interface PasswordManagementPanelProps {
  forced?: boolean;
  onChanged?: () => void;
}
interface PasswordFormValues {
  old_password: string;
  new_password: string;
  confirm_password: string;
}

export function PasswordManagementPanel({
  forced = false,
  onChanged,
}: PasswordManagementPanelProps) {
  const [form] = Form.useForm<PasswordFormValues>();
  const [saving, setSaving] = useState(false);
  const authUser = useAuthStore((s) => s.authUser);
  const setAuthUser = useAuthStore((s) => s.setAuthUser);

  const handleSubmit = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      await changeMyPassword(values.old_password, values.new_password);
      if (authUser) {
        setAuthUser({ ...authUser, must_change_password: false });
      }
      form.resetFields();
      message.success(t('密码修改成功'));
      onChanged?.();
    } catch (error) {
      const raw = (error as Error).message || '密码修改失败';
      message.error(t(raw));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="jx-passwordPanel">
      {forced ? (
        <Alert
          type="warning"
          showIcon
          message={t('首次登录必须修改默认密码')}
          description={t('默认账号使用临时密码 admin。为确保实例安全，请设置新密码后继续使用 HugAgentOS。')}
          style={{ marginBottom: 16 }}
        />
      ) : (
        <Typography.Paragraph type="secondary">
          {t('定期更新登录密码可以保护你的账号与实例配置。')}
        </Typography.Paragraph>
      )}
      <Form form={form} layout="vertical" autoComplete="off">
        <Form.Item
          name="old_password"
          label={t('当前密码')}
          rules={[{ required: true, message: t('请输入当前密码') }]}
        >
          <Input.Password autoComplete="current-password" />
        </Form.Item>
        <Form.Item
          name="new_password"
          label={t('新密码')}
          rules={[
            { required: true, message: t('请输入新密码') },
            { min: 8, message: t('新密码至少 8 位') },
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          name="confirm_password"
          label={t('确认新密码')}
          dependencies={['new_password']}
          rules={[
            { required: true, message: t('请再次输入新密码') },
            ({ getFieldValue }) => ({
              validator(_, value: string) {
                if (!value || getFieldValue('new_password') === value) return Promise.resolve();
                return Promise.reject(new Error(t('两次输入的新密码不一致')));
              },
            }),
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Button type="primary" loading={saving} onClick={() => void handleSubmit()}>
          {forced ? t('修改密码并继续') : t('更新密码')}
        </Button>
      </Form>
    </div>
  );
}
