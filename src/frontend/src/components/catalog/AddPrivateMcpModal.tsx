import { useCallback, useRef, useState } from 'react';
import { Collapse, Form, Input, Modal, Radio, Select, Typography, message } from 'antd';
import { t } from '../../i18n';
import { cancelMcpMarketOAuth, createMyMcpServer, startMyMcpOAuth } from '../../api';
import { OAUTH_CANCELLED, runMcpOAuthPopup, type McpOAuthAttempt } from './mcpOAuthPopup';

interface AddPrivateMcpModalProps {
  open: boolean;
  onClose: () => void;
  onAdded: () => void | Promise<void>;
}

type AuthKind = 'none' | 'header' | 'oauth';

function parseHeaders(text: unknown): Record<string, string> {
  const headers: Record<string, string> = {};
  if (typeof text !== 'string') return headers;
  text.split('\n').forEach((line) => {
    const idx = line.indexOf('=');
    if (idx > 0) headers[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
  });
  return headers;
}

const OAUTH_STAGE_TEXT: Record<string, string> = {
  starting: '正在连接授权服务器…',
  waiting_for_user: '请在新标签页完成登录并同意授权…',
  processing_callback: '已收到授权，正在验证…',
  installing: '正在保存连接器…',
};

export function AddPrivateMcpModal({ open, onClose, onAdded }: AddPrivateMcpModalProps) {
  const [form] = Form.useForm();
  const auth = (Form.useWatch('auth', form) as AuthKind | undefined) || 'none';
  const [adding, setAdding] = useState(false);
  const [stage, setStage] = useState('');
  const attemptRef = useRef<McpOAuthAttempt | null>(null);

  const close = useCallback(() => {
    const attempt = attemptRef.current;
    if (attempt) {
      attempt.cancelled = true;
      attempt.popup?.close();
      if (attempt.flowId) void cancelMcpMarketOAuth(attempt.flowId).catch(() => undefined);
    }
    setAdding(false);
    onClose();
  }, [onClose]);

  const submit = useCallback(async () => {
    // Open the consent tab inside the click, before any await, or browsers block it.
    const popup = auth === 'oauth' ? window.open('about:blank', '_blank') : null;
    let values;
    try {
      values = await form.validateFields();
    } catch {
      popup?.close();
      return;
    }
    const base = {
      display_name: values.display_name,
      transport: values.transport,
      url: values.url,
      description: values.description || '',
      headers: parseHeaders(values.headers_text),
    };
    const attempt: McpOAuthAttempt | null = auth === 'oauth' ? { flowId: '', popup, cancelled: false } : null;
    attemptRef.current = attempt;
    setAdding(true);
    setStage(auth === 'oauth' ? 'starting' : '');
    try {
      if (attempt) {
        const started = await startMyMcpOAuth({
          ...base,
          scopes: values.scopes || [],
          client_id: values.oauth_client_id || '',
          client_secret: values.oauth_client_secret || '',
        });
        await runMcpOAuthPopup(attempt, started, setStage);
      } else {
        await createMyMcpServer(base);
      }
      message.success(t('已添加'));
      form.resetFields();
      onClose();
      await onAdded();
    } catch (error) {
      popup?.close();
      const reason = (error as Error).message;
      if (attempt?.cancelled || reason === OAUTH_CANCELLED) {
        message.info(t('OAuth 登录已取消'));
      } else {
        message.error(reason || t('添加失败'));
      }
    } finally {
      if (attemptRef.current === attempt) attemptRef.current = null;
      setStage('');
      setAdding(false);
    }
  }, [auth, form, onAdded, onClose]);

  return (
    <Modal
      title={t('添加连接器')}
      open={open}
      onCancel={close}
      onOk={() => void submit()}
      okText={auth === 'oauth' ? t('授权并添加') : t('添加')}
      cancelText={t('取消')}
      confirmLoading={adding}
      destroyOnHidden
    >
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        {t('支持公网 HTTP/HTTPS 的远程 HTTP/SSE MCP，添加时会执行地址安全检查和工具发现。HTTP 为明文传输，生产环境建议使用 HTTPS。该连接仅你自己可见可用，之后可申请上架市场。')}
      </Typography.Paragraph>
      {stage && OAUTH_STAGE_TEXT[stage] && (
        <Typography.Paragraph type="secondary">{t(OAUTH_STAGE_TEXT[stage])}</Typography.Paragraph>
      )}
      <Form form={form} layout="vertical" initialValues={{ transport: 'streamable_http', auth: 'none' }}>
        <Form.Item name="display_name" label={t('名称')} rules={[{ required: true, message: t('请输入名称') }]}>
          <Input placeholder="如「我的天气服务」" maxLength={255} />
        </Form.Item>
        <Form.Item name="transport" label={t('类型')} rules={[{ required: true }]}>
          <Select
            options={[
              { label: 'Streamable HTTP', value: 'streamable_http' },
              { label: 'SSE', value: 'sse' },
            ]}
          />
        </Form.Item>
        <Form.Item name="url" label={t('服务地址 URL')} rules={[{ required: true, message: t('请输入 URL') }]}>
          <Input placeholder="http://example.com:8080/mcp 或 https://example.com/mcp" />
        </Form.Item>
        <Form.Item name="description" label={t('描述（可选）')}>
          <Input.TextArea rows={2} maxLength={2000} placeholder={t('简单说明这个工具的用途')} />
        </Form.Item>
        <Form.Item name="auth" label={t('鉴权方式')}>
          <Radio.Group>
            <Radio value="none">{t('无需鉴权')}</Radio>
            <Radio value="header">{t('请求头 / Token')}</Radio>
            <Radio value="oauth">OAuth</Radio>
          </Radio.Group>
        </Form.Item>
        {auth === 'oauth' && (
          <Typography.Paragraph type="secondary">
            {t('点击授权并添加后，在新标签页登录并同意授权；令牌加密保存，只属于你。')}
          </Typography.Paragraph>
        )}
        {auth !== 'none' && (
          <Form.Item
            name="headers_text"
            label={auth === 'oauth' ? t('额外请求头（可选）') : t('请求头')}
            rules={auth === 'header' ? [{ required: true, message: t('请填写认证请求头') }] : []}
            help={auth === 'oauth'
              ? t('随每次请求额外发送的头，每行一个：Key=Value；一般无需填写。')
              : t('每行一个：Key=Value。需要鉴权的服务在此填认证头，如 Authorization=Bearer xxx')}
          >
            <Input.TextArea rows={2} placeholder={auth === 'oauth' ? 'X-Tenant=acme' : 'Authorization=Bearer xxx'} />
          </Form.Item>
        )}
        {auth === 'oauth' && (
          <Collapse
            ghost
            size="small"
            items={[{
              key: 'advanced',
              label: t('高级：Scopes 与自己的 OAuth App（可选）'),
              children: (
                <>
                  <Form.Item name="scopes" label={t('Scopes（可选）')} extra={t('留空使用授权服务器默认权限。')}>
                    <Select mode="tags" tokenSeparators={[' ', ',']} placeholder="read mcp" />
                  </Form.Item>
                  <Typography.Paragraph type="secondary">
                    {t('仅当服务方不支持自动注册时需要。回调地址：{url}', { url: `${window.location.origin}/api/v1/mcp-market/oauth/callback` })}
                  </Typography.Paragraph>
                  <Form.Item name="oauth_client_id" label="OAuth Client ID"><Input autoComplete="off" /></Form.Item>
                  <Form.Item name="oauth_client_secret" label="OAuth Client Secret">
                    <Input.Password autoComplete="new-password" />
                  </Form.Item>
                </>
              ),
            }]}
          />
        )}
      </Form>
    </Modal>
  );
}
