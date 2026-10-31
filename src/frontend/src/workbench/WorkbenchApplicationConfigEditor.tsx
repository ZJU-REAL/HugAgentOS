import { useEffect, useRef, useState } from 'react';
import { Modal } from 'antd';
import { t, getLang } from '../i18n';
import { ApiResponseError } from '../utils/apiError';
import {
  applicationConfigPath, applicationConfigRequest, configurableApplications, configurationValues,
  type ConfigurableApplication,
} from './applicationConfigApi';
import {HostButton, HostCheckbox, HostField, HostFieldset, HostHint, HostInput, HostNotice, HostSection, HostSelect, HostToolbar} from './ui';
import {SkeletonLines} from '../components/common/LoadState';

function itemTitle(item: ConfigurableApplication) {
  if (typeof item.title === 'string') return item.title;
  return item.title[getLang()] || item.title.en || item.title['zh-CN'] || Object.values(item.title)[0] || '';
}
function configError(error: unknown, saving = false) {
  if (error instanceof ApiResponseError) {
    if (error.status === 401 || error.status === 403) return t('请使用具有系统配置权限的账号重新登录');
    if (error.status === 409) return t('应用配置版本已改变，请重新加载后再保存');
  }
  return t(saving ? '工作台应用配置保存失败，请重试' : '工作台应用配置加载失败，请重试');
}
function InstalledConfiguration({ token, item }: { token: string; item: ConfigurableApplication }) {
  const [config, setConfig] = useState<ConfigurableApplication | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [clearKeys, setClearKeys] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [generation, setGeneration] = useState(0);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    request.current = controller;
    void applicationConfigRequest<ConfigurableApplication>(token,
      applicationConfigPath(item) + '?revision=' + item.revision, { signal: controller.signal })
      .then(result => {
        if (controller.signal.aborted) return;
        setConfig(result);
        setValues(Object.fromEntries(result.fields.filter(field => !field.secret)
          .map(field => [field.key, field.value || ''])));
      }).catch(reason => { if (!controller.signal.aborted) setError(configError(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => request.current?.abort();
  }, [token, item, generation]);
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!config || busy) return;
    const controller = new AbortController();
    request.current = controller; setBusy(true); setError(''); setSaved(false);
    try {
      const result = await applicationConfigRequest<ConfigurableApplication>(token, applicationConfigPath(config), {
        method: 'PUT', signal: controller.signal,
        body: JSON.stringify({ revision: config.revision,
          values: configurationValues(config.fields, values, clearKeys),
          ...(clearKeys.length ? { clearKeys } : {}) }),
      });
      if (!controller.signal.aborted) {
        setConfig(result); setClearKeys([]);
        setValues(Object.fromEntries(result.fields.filter(field => !field.secret)
          .map(field => [field.key, field.value || ''])));
        setSaved(true);
      }
    } catch (reason) {
      if (!controller.signal.aborted) setError(configError(reason, true));
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  function toggleClear(key: string, name: string, checked: boolean) {
    const apply = () => {
      setClearKeys(previous => checked ? [...previous, key] : previous.filter(value => value !== key));
      setSaved(false);
    };
    if (!checked) {apply(); return;}
    Modal.confirm({
      title: t('确认清除配置项：{name}？保存后生效。', { name }), okText: t('清除'), cancelText: t('取消'),
      okButtonProps: { danger: true }, onOk: apply,
    });
  }
  return <form aria-label={t('工作台应用配置表单')} onSubmit={event => void save(event)} className="jx-hostSection">
    {loading && <SkeletonLines rows={3}/>}
    {error && <HostNotice tone="error">{error}</HostNotice>}
    {saved && <HostNotice tone="success">{t('工作台应用配置已保存')}</HostNotice>}
    <HostFieldset disabled={loading || busy || !config} legend={itemTitle(item)}>
      {config?.fields.map(field => <div key={field.key} className="jx-hostFieldGroup">
        <HostField label={field.label || field.key} hint={field.description}>
          <HostInput aria-label={field.label || field.key} type={field.secret ? 'password' : 'text'}
            autoComplete={field.secret ? 'new-password' : 'off'} disabled={clearKeys.includes(field.key)}
            value={values[field.key] || ''} onChange={event => {
              setValues(previous => ({ ...previous, [field.key]: event.target.value })); setSaved(false);
            }} placeholder={field.secret && field.is_set ? t('已配置，留空则保留不变') : ''} />
        </HostField>
        {field.is_set && <HostCheckbox checked={clearKeys.includes(field.key)}
          onChange={event => toggleClear(field.key, field.label || field.key, event.target.checked)}>{t('清除此配置项')}</HostCheckbox>}
      </div>)}
      <HostToolbar>
        <HostButton tone="primary" submit>{busy ? t('正在保存…') : t('保存应用配置')}</HostButton>
        <HostButton tone="text" disabled={busy} onClick={() => {
          setLoading(true); setConfig(null); setError(''); setValues({}); setClearKeys([]); setSaved(false);
          setGeneration(value => value + 1);
        }}>{t('重新加载应用配置')}</HostButton>
      </HostToolbar>
    </HostFieldset>
    <HostHint>{t('凭据仅用于当前安装的应用；密钥留空保留原值。清除操作在保存后生效。')}</HostHint>
  </form>;
}
function ConfigurationManager({ token }: { token: string }) {
  const [items, setItems] = useState<ConfigurableApplication[]>([]);
  const [selected, setSelected] = useState('');
  const [generation, setGeneration] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    void configurableApplications(token, controller.signal)
      .then(result => { if (!controller.signal.aborted) setItems(result); })
      .catch(reason => { if (!controller.signal.aborted) setError(configError(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [token, generation]);
  const item = items.find(value => value.applicationKey === selected);
  return <HostSection title={t('工作台应用配置')} description={t('选择已安装的应用，配置此服务器上的连接与凭据。')}
    actions={<HostButton tone="text" onClick={() => {
      setLoading(true); setItems([]); setSelected(''); setError(''); setGeneration(value => value + 1);
    }}>{t('刷新已安装应用')}</HostButton>}>
    {error && <HostNotice tone="error">{error}</HostNotice>}
    <HostField label={t('已安装的工作台应用')}>
      <HostSelect aria-label={t('已安装的工作台应用')} disabled={loading} value={selected}
        onChange={event => setSelected(event.target.value)}>
        <option value="">{t('请选择应用')}</option>
        {items.map(value => <option key={value.applicationKey} value={value.applicationKey}>
          {itemTitle(value)} · {t(value.origin.kind === 'local' ? '本机' : '云端')} ·
          {t('所有者：{name}', { name: value.ownerUserId || t('全局安装') })}
        </option>)}
      </HostSelect>
    </HostField>
    {loading && <SkeletonLines rows={3}/>}
    {!loading && !error && !items.length && <HostNotice>{t('没有可配置的工作台应用')}</HostNotice>}
    {item && <InstalledConfiguration key={token + ':' + item.applicationKey + ':' + item.revision} token={token} item={item} />}
  </HostSection>;
}

export function WorkbenchApplicationConfigEditor({ token }: { token: string }) {
  return <ConfigurationManager key={token} token={token} />;
}
