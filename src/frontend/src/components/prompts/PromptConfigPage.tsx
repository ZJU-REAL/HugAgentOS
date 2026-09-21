import { useState } from 'react';
import { Alert, Button, Input, Space, Typography } from 'antd';
import { PromptsEditor } from './PromptsEditor';
import { promptFetch } from './promptApi';
import { t } from '../../i18n';

export default function PromptConfigPage() {
  const [token, setToken] = useState('');
  const [authorized, setAuthorized] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const enter = async () => {
    setLoading(true);
    try { await promptFetch(token, '/v1/admin/prompts/kinds'); setAuthorized(true); setError(''); }
    catch (e) { setError((e as Error).message); }
    finally { setLoading(false); }
  };
  return <main style={{ padding: 24, maxWidth: 1440, margin: '0 auto' }}>
    <Space style={{ marginBottom: 16 }} wrap>
      <Typography.Title level={3} style={{ margin: 0 }}>{t('提示词管理')}</Typography.Title>
      <Button href="/">{t('返回')}</Button>
    </Space>
    {authorized ? <PromptsEditor token={token} fetchFn={promptFetch} /> : <Space direction="vertical" style={{ width: '100%', maxWidth: 480 }}>
      <Alert type="info" showIcon message={t('使用当前管理员登录，或输入 Config Token 管理提示词。')} />
      <Input.Password value={token} onChange={e => setToken(e.target.value)} placeholder="Config Token" onPressEnter={enter} />
      <Button type="primary" loading={loading} onClick={enter}>{t('进入提示词管理')}</Button>
      {error && <Alert type="error" showIcon message={error} />}
    </Space>}
  </main>;
}
