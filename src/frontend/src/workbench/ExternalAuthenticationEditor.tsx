import { useCallback, useEffect, useRef, useState } from 'react';
import { configFetch } from '../utils/adminApi';
import { applicationText, type ExternalApplication } from './externalApi';
import { t } from '../i18n';

type Mode = 'independent' | 'shared_identity' | 'exchange';
type AuthConfig = {
  revision: number; mode: Mode; identityProvider?: string; loginUrl?: string;
  audience?: string; callback?: string; initiationUrl?:string; ttlSeconds?: number; requiresConfiguration?:boolean;
};
const initial: AuthConfig = {revision:0, mode:'independent'};
const endpoint = '/v1/workbench/admin/external-auth/';

function AuthenticationForm({token, applicationKey}: {token:string; applicationKey:string}) {
  const [config, setConfig] = useState<AuthConfig>(initial);
  const [secret, setSecret] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true); setError(''); setSecret('');
    try {
      const result = await configFetch(token, endpoint + encodeURIComponent(applicationKey), {signal:controller.signal});
      if (!controller.signal.aborted) setConfig(result.data as AuthConfig);
    } catch {
      if (!controller.signal.aborted) setError('认证配置加载失败，请重试');
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [token, applicationKey]);
  useEffect(() => {void load(); return () => request.current?.abort();}, [load]);
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (saving) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setSaving(true); setMessage(''); setError('');
    const body = {
      revision:config.revision, mode:config.mode,
      ...(config.mode === 'shared_identity' ? {identityProvider:config.identityProvider, loginUrl:config.loginUrl} : {}),
      ...(config.mode === 'exchange' ? {
        audience:config.audience, callback:config.callback, initiationUrl:config.initiationUrl, ttlSeconds:config.ttlSeconds ?? 30,
        ...(secret ? {clientSecret:secret} : {}),
      } : {}),
    };
    try {
      const result = await configFetch(token, endpoint + encodeURIComponent(applicationKey), {
        method:'PUT', body:JSON.stringify(body), signal:controller.signal,
      });
      if (!controller.signal.aborted) {
        setConfig(result.data as AuthConfig); setSecret(''); setMessage('认证配置已保存');
      }
    } catch {
      if (!controller.signal.aborted) setError('认证配置保存失败或版本冲突，请重新加载');
    } finally {
      if (!controller.signal.aborted) setSaving(false);
    }
  }
  return <form onSubmit={event => void save(event)} aria-label={t('外部认证配置')}>
    {error && <p role="alert">{t(error)}</p>}
    {message && <p role="status">{t(message)}</p>}
    {loading && <p role="status">{t('正在加载…')}</p>}
    {config.requiresConfiguration && <p role="status">{t('旧应用尚未配置认证；请审核目标后显式选择认证方式')}</p>}
    <button type="button" disabled={saving} onClick={() => void load()}>{t('重新加载认证配置')}</button>
    <fieldset disabled={loading || saving} style={{display:'grid',gap:12}}>
      <legend>{t('认证方式')}</legend>
      <label>{t('认证方式')}<select aria-label={t("认证方式")} value={config.mode} onChange={e => {
        setConfig({...config, mode:e.target.value as Mode}); setSecret('');
      }}>
        <option value="independent">{t('独立登录')}</option>
        <option value="shared_identity">{t('共同身份提供方')}</option>
        <option value="exchange">{t('一次性换票')}</option>
      </select></label>
      {config.mode === 'shared_identity' && <>
        <label>{t('身份提供方标识')}<input required maxLength={200} value={config.identityProvider ?? ''} onChange={e => setConfig({...config,identityProvider:e.target.value})}/></label>
        <label>{t('目标系统登录 URL')}<input required type="url" value={config.loginUrl ?? ''} onChange={e => setConfig({...config,loginUrl:e.target.value})}/></label>
        <p>{t('目标系统自行接入同一身份提供方。宿主不会转发 Cookie 或通用令牌。')}</p>
      </>}
      {config.mode === 'exchange' && <>
        <label>{t('目标登录发起 URL')}<input required type="url" value={config.initiationUrl ?? ''} onChange={e => setConfig({...config,initiationUrl:e.target.value})}/></label>
        <label>{t('受众标识')}<input required maxLength={200} value={config.audience ?? ''} onChange={e => setConfig({...config,audience:e.target.value})}/></label>
        <label>{t('精确回调 URL')}<input required type="url" value={config.callback ?? ''} onChange={e => setConfig({...config,callback:e.target.value})}/></label>
        <label>{t('目标服务端密钥')}<input type="password" autoComplete="new-password" minLength={32} maxLength={256} value={secret} onChange={e => setSecret(e.target.value)}/></label>
        <p>{t('使用至少 32 字符的随机密钥；留空保留现有密钥。密钥仅保存哈希，不能读取。')}</p>
        <label>{t('票据有效秒数')}<input type="number" min={1} max={60} value={config.ttlSeconds ?? 30} onChange={e => setConfig({...config,ttlSeconds:Number(e.target.value)})}/></label>
      </>}
      <button type="submit">{saving ? t('正在保存…') : t('保存认证配置')}</button>
    </fieldset>
    <p>{t('宿主退出会使未兑换的会话票据失效。第三方会话退出由目标系统处理。')}</p>
  </form>;
}

export function ExternalAuthenticationManager({token}: {token:string}) {
  const [items, setItems] = useState<ExternalApplication[]>([]);
  const [key, setKey] = useState('');
  const [error, setError] = useState('');
  const [generation, setGeneration] = useState(0);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    void configFetch(token, '/v1/workbench/admin/catalog', {signal:controller.signal})
      .then(result => {
        if (!controller.signal.aborted) setItems((result.data.items as ExternalApplication[])
          .filter(item => item.declaration.target.kind === 'external'));
      }).catch(() => {
        if (!controller.signal.aborted) setError('认证应用列表加载失败，请重试');
      }).finally(() => {if (!controller.signal.aborted) setLoading(false);});
    return () => controller.abort();
  }, [token, generation]);
  return <section aria-label={t('外部应用认证')}>
    <h3>{t('外部应用认证')}</h3>
    {error && <p role="alert">{t(error)}</p>}
    <button type="button" onClick={() => {setLoading(true); setItems([]); setKey(''); setError(''); setGeneration(value => value + 1);}}>{t('刷新认证应用列表')}</button>
    <label>{t('认证应用')}<select disabled={loading} aria-label={t("认证应用")} value={key} onChange={e => setKey(e.target.value)}>
      <option value="">{t('请选择应用')}</option>
      {items.map(item => <option key={item.applicationKey} value={item.applicationKey}>
        {applicationText(item.declaration.title)} ({item.applicationKey})
      </option>)}
    </select></label>
    {loading && <p role="status">{t('正在加载…')}</p>}
    {!loading && !error && items.length === 0 && <p>{t('没有可配置认证的外部应用')}</p>}
    {key && <AuthenticationForm key={token + ':' + key} token={token} applicationKey={key}/>}
  </section>;
}
