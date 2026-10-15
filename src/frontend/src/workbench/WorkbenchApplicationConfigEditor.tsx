import { useEffect, useRef, useState } from 'react';
import { t, getLang } from '../i18n';
import { ApiResponseError } from '../utils/apiError';
import {
  applicationConfigPath, applicationConfigRequest, configurableApplications, configurationValues,
  type ConfigurableApplication,
} from './applicationConfigApi';

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
  return <form aria-label={t('工作台应用配置表单')} onSubmit={event => void save(event)}>
    {loading && <p role="status">{t('正在加载…')}</p>}
    {error && <p role="alert">{error}</p>}
    {saved && <p role="status">{t('工作台应用配置已保存')}</p>}
    <button type="button" disabled={busy} onClick={() => {
      setLoading(true); setConfig(null); setError(''); setValues({}); setClearKeys([]); setSaved(false);
      setGeneration(value => value + 1);
    }}>
      {t('重新加载应用配置')}</button>
    <fieldset disabled={loading || busy || !config} style={{ display: 'grid', gap: 12, marginTop: 12 }}>
      <legend>{itemTitle(item)}</legend>
      {config?.fields.map(field => <div key={field.key}>
        <label style={{ display: 'grid', gap: 4 }}>{field.label || field.key}
          <input aria-label={field.label || field.key} type={field.secret ? 'password' : 'text'}
            autoComplete={field.secret ? 'new-password' : 'off'} disabled={clearKeys.includes(field.key)}
            value={values[field.key] || ''} onChange={event => {
              setValues(previous => ({ ...previous, [field.key]: event.target.value })); setSaved(false);
            }} placeholder={field.secret && field.is_set ? t('已配置，留空则保留不变') : ''} />
        </label>
        {field.description && <p>{field.description}</p>}
        {field.is_set && <label><input type="checkbox" checked={clearKeys.includes(field.key)}
          onChange={event => {
            const checked = event.target.checked;
            if (checked && !window.confirm(t('确认清除配置项：{name}？保存后生效。', { name: field.label || field.key }))) return;
            setClearKeys(previous => checked ? [...previous, field.key] : previous.filter(key => key !== field.key));
            setSaved(false);
          }} />{t('清除此配置项')}</label>}
      </div>)}
      <button type="submit">{busy ? t('正在保存…') : t('保存应用配置')}</button>
    </fieldset>
    <p>{t('凭据仅用于当前安装的应用；密钥留空保留原值。清除操作在保存后生效。')}</p>
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
  return <section aria-label={t('工作台应用配置')} style={{ marginTop: 24 }}>
    <h3>{t('工作台应用配置')}</h3>
    <p>{t('选择已安装的应用，配置此服务器上的连接与凭据。')}</p>
    {error && <p role="alert">{error}</p>}
    <button type="button" onClick={() => {
      setLoading(true); setItems([]); setSelected(''); setError(''); setGeneration(value => value + 1);
    }}>{t('刷新已安装应用')}</button>
    <label style={{ display: 'grid', gap: 8, margin: '12px 0' }}>{t('已安装的工作台应用')}
      <select aria-label={t('已安装的工作台应用')} disabled={loading} value={selected}
        onChange={event => setSelected(event.target.value)}>
        <option value="">{t('请选择应用')}</option>
        {items.map(value => <option key={value.applicationKey} value={value.applicationKey}>
          {itemTitle(value)} · {t(value.origin.kind === 'local' ? '本机' : '云端')} ·
          {t('所有者：{name}', { name: value.ownerUserId || t('全局安装') })}
        </option>)}
      </select>
    </label>
    {loading && <p role="status">{t('正在加载…')}</p>}
    {!loading && !error && !items.length && <p>{t('没有可配置的工作台应用')}</p>}
    {item && <InstalledConfiguration key={token + ':' + item.applicationKey + ':' + item.revision} token={token} item={item} />}
  </section>;
}

export function WorkbenchApplicationConfigEditor({ token }: { token: string }) {
  return <ConfigurationManager key={token} token={token} />;
}
