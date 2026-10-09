import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, message, Modal, Popover, Select, Space, Upload } from 'antd';
import { CloudUploadOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { ADMIN_STORAGE_KEY, API_BASE, CONFIG_STORAGE_KEY } from '../../utils/adminApi';
import { createApiResponseError } from '../../utils/apiError';

type Kind = 'skill' | 'plugin' | 'agent' | 'connector';
interface Versions {
  versions: { id: string; version: string; current: boolean }[];
  revision: string;
  highest_version: string;
}

export function MarketVersionControls({ kind, slug, token, onChanged }: {
  kind: Kind; slug: string; token?: string; onChanged: () => Promise<void>;
}) {
  const [data, setData] = useState<Versions>();
  const [error, setError] = useState('');
  const [file, setFile] = useState<File>();
  const [bump, setBump] = useState('patch');
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const lock = useRef(false);
  const endpoint = `/v1/admin/marketplace-versions/${kind}/${encodeURIComponent(slug)}`;
  const request = useCallback(async (suffix = '', init?: RequestInit) => {
    const auth = token ?? localStorage.getItem(kind === 'connector' ? CONFIG_STORAGE_KEY : ADMIN_STORAGE_KEY) ?? '';
    const res = await fetch(API_BASE + endpoint + suffix, {
      ...init, credentials: 'include',
      headers: { ...(auth ? { Authorization: `Bearer ${auth}` } : {}), ...init?.headers },
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw createApiResponseError(res.status, body, `HTTP ${res.status}`);
    return body.data as Versions;
  }, [endpoint, kind, token]);
  const load = useCallback(async () => {
    try { setData(await request()); setError(''); }
    catch (e) { setError((e as Error).message); }
  }, [request]);
  useEffect(() => { void load(); }, [load]);
  const run = async (action: () => Promise<Versions>) => {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError('');
    try {
      setData(await action());
      setFile(undefined); setOpen(false);
      message.success(t('市场版本已更新'));
      await onChanged();
    } catch (e) { setError((e as Error).message); }
    finally { lock.current = false; setBusy(false); }
  };
  const upload = () => {
    if (!file || !data) return;
    const form = new FormData();
    form.append('file', file); form.append('bump', bump);
    form.append('expected_revision', data.revision);
    void run(() => request('', { method: 'POST', body: form }));
  };
  const select = (id: string) => {
    if (!data || data.versions.find(v => v.current)?.id === id) return;
    const selected = data.versions.find(v => v.id === id);
    Modal.confirm({
      title: t('切换到版本 {version}？', { version: selected?.version || '' }),
      content: t('市场将恢复该版本的完整内容。'),
      onOk: () => run(() => request('/select', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version_id: id, expected_revision: data.revision }),
      })),
    });
  };
  return (
    <Space size={6} wrap data-testid="market-version-controls">
      <Select aria-label={t('选择版本')} size="small" style={{ minWidth: 108 }}
        value={data?.versions.find(v => v.current)?.id} disabled={busy || !data}
        onChange={select} options={data?.versions.map(v => ({
          value: v.id, label: `v${v.version}${v.current ? ' · ' + t('当前') : ''}`,
        }))} />
      <Popover trigger="click" open={open} onOpenChange={setOpen} title={t('更新版本')}
        content={<Space direction="vertical" style={{ width: 300 }}>
          <span>{t('ZIP ≤20 MiB；解压 ≤50 MiB；禁止凭据文件。')}</span>
          <Button size="small" onClick={async () => {
            const auth = token ?? localStorage.getItem(kind === 'connector' ? CONFIG_STORAGE_KEY : ADMIN_STORAGE_KEY) ?? '';
            const res = await fetch(API_BASE + endpoint + '/package', {
              credentials: 'include', headers: auth ? { Authorization: `Bearer ${auth}` } : {},
            });
            if (!res.ok) { setError(t('下载失败')); return; }
            const url = URL.createObjectURL(await res.blob());
            const a = document.createElement('a'); a.href = url; a.download = slug + '.zip'; a.click();
            URL.revokeObjectURL(url);
          }}>{t('下载当前版本包')}</Button>
          <Select aria-label={t('版本递增方式')} value={bump} onChange={setBump} style={{ width: '100%' }}
            options={[{ value: 'patch', label: t('补丁版本 +1') },
              { value: 'minor', label: t('次版本 +1') }, { value: 'major', label: t('主版本 +1') }]} />
          <Upload accept=".zip" maxCount={1} fileList={file ? [{
            uid: slug, name: file.name, status: 'done',
          }] : []} onRemove={() => { setFile(undefined); }}
            beforeUpload={f => {
              if (!f.name.toLowerCase().endsWith('.zip') || f.size > 20 * 1024 * 1024) {
                setError(t('请选择不超过 20 MiB 的 ZIP 包')); return Upload.LIST_IGNORE;
              }
              setError(''); setFile(f); return false;
            }}><Button icon={<CloudUploadOutlined />}>{t('选择 ZIP')}</Button></Upload>
          {error && <Alert type="error" message={error} />}
          <Button type="primary" loading={busy} disabled={!file || !data} onClick={upload}>{t('上传并更新')}</Button>
          {error && <Button size="small" onClick={() => void load()}>{t('刷新版本')}</Button>}
        </Space>}>
        <Button size="small" disabled={busy} onClick={() => setOpen(true)}>{t('更新版本')}</Button>
      </Popover>
      {error && !open && <span role="alert" style={{ color: 'var(--color-error)' }}>{error}</span>}
    </Space>
  );
}
