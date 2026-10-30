import { useCallback, useEffect, useRef, useState } from 'react';
import type { ApplicationDeclaration, I18nText } from '@hugagent/workbench-protocol';
import { validateApplication } from '@hugagent/workbench-protocol';
import { configFetch } from '../utils/adminApi';
import { externalAdminPath, applicationText, saveExternal, externalErrorMessage, type ExternalApplication } from './externalApi';
import type { EmbeddingPolicy } from './externalEmbed';
import { EmbedPolicyFields } from './EmbedPolicyFields';
import { getLang, t } from '../i18n';
import { ExternalAuthenticationManager } from './ExternalAuthenticationEditor';
import {HostButton, HostCheckbox, HostField, HostFieldset, HostHint, HostInput, HostMeta, HostNotice, HostSection, HostSelect, HostToolbar} from './ui';

const empty = {id:'', title:'', description:'', url:'https://', enabled:true};
const defaultEmbedding: EmbeddingPolicy = {origin:'https://', ready:'unconfirmed', allowForms:false, originStorage:false};

function editText(previous: I18nText | undefined, value: string): I18nText | undefined {
  if (!previous || typeof previous === 'string') return value || undefined;
  if (applicationText(previous) === value) return previous;
  const locale = getLang() in previous ? getLang() : 'en' in previous ? 'en' : Object.keys(previous)[0];
  const updated = {...previous};
  if (value) updated[locale] = value;
  else delete updated[locale];
  return Object.keys(updated).length ? updated : undefined;
}
export function ExternalApplicationEditor({token}: {token: string}) {
  const [items, setItems] = useState<ExternalApplication[]>([]);
  const [selected, setSelected] = useState<ExternalApplication | null>(null);
  const [form, setForm] = useState(empty);
  const [mode, setMode] = useState<'browser' | 'embedded'>('browser');
  const [embedding, setEmbedding] = useState(defaultEmbedding);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const request = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError('');
    try {
      const response = await configFetch(token, externalAdminPath, {signal: controller.signal});
      if (!Array.isArray(response.data?.items)) throw new Error(t('应用目录格式无效'));
      if (!controller.signal.aborted) setItems(response.data.items);
    } catch (e) {
      if (!controller.signal.aborted) setError(e instanceof Error ? t(e.message) : t('加载失败'));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [token]);
  useEffect(() => {void load(); return () => request.current?.abort();}, [load]);
  function edit(item: ExternalApplication | null) {
    setSelected(item); setError(''); setNotice('');
    setMode(item?.declaration.target.kind === 'external' ? item.declaration.target.mode : 'browser');
    setEmbedding(item?.embedding || defaultEmbedding);
    setForm(item ? {
      id:item.declaration.id, title:applicationText(item.declaration.title),
      description:applicationText(item.declaration.description),
      url:item.declaration.target.kind === 'external' ? item.declaration.target.url : '',
      enabled:item.enabled,
    } : empty);
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (saving) return;
    const description = editText(selected?.declaration.description, form.description);
    const declaration: ApplicationDeclaration = {
      schemaVersion:'1.0.0', id:form.id, title:editText(selected?.declaration.title, form.title) || '',
      ...(description === undefined ? {} : {description}),
      target:{kind:'external', mode, url:form.url},
    };
    if (validateApplication(declaration).length) {setError('请检查应用 ID、名称和 HTTP 地址'); return;}
    setSaving(true); setError(''); setNotice('');
    try {
      const item = await saveExternal(token, selected, declaration, form.enabled, mode === 'embedded' ? embedding : undefined);
      setSelected(item);
      await load();
      setNotice('外部应用已保存');
    } catch (e) {
      setError(externalErrorMessage(e));
    } finally {setSaving(false);}
  }
  return <div className="jx-hostSections">
    <HostSection level={2} title={t('普通外部应用')}
      description={t('独立登录，在浏览器新窗口打开。不会传递宿主登录凭据。旧应用配置保留在下方。')}
      actions={<>
        <HostButton onClick={() => edit(null)} disabled={saving}>{t('新增外部应用')}</HostButton>
        <HostButton tone="text" onClick={() => void load()} disabled={saving}>{t('重新加载')}</HostButton>
      </>}>
      {loading && <HostNotice tone="loading">{t('正在加载应用目录…')}</HostNotice>}
      {error && <HostNotice tone="error">{t(error)}</HostNotice>}
      {notice && <HostNotice tone="success">{t(notice)}</HostNotice>}
      <ul className="jx-hostList">{items.map(item => <li key={item.applicationKey} className="jx-hostList-row">
        <span className="jx-hostList-title">
          <HostButton tone="text" size="small" disabled={saving} onClick={() => edit(item)}>{applicationText(item.declaration.title)}</HostButton>
        </span>
        <HostMeta>{item.enabled ? t('启用') : t('禁用')}{' · revision '}{item.revision}</HostMeta>
      </li>)}</ul>
      <form onSubmit={event => void save(event)}>
        <HostFieldset disabled={saving || loading} legend={selected ? t('编辑外部应用') : t('新增外部应用')}>
          <HostField label={t('应用 ID')}><HostInput required maxLength={64} pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,63}" disabled={!!selected} value={form.id} onChange={e => setForm({...form,id:e.target.value})}/></HostField>
          <HostField label={t('应用名称')}><HostInput required maxLength={400} value={form.title} onChange={e => setForm({...form,title:e.target.value})}/></HostField>
          <HostField label={t('应用简介')}><HostInput maxLength={400} value={form.description} onChange={e => setForm({...form,description:e.target.value})}/></HostField>
          <HostField label={t('跳转 URL')}><HostInput required type="url" maxLength={2048} value={form.url} onChange={e => setForm({...form,url:e.target.value})}/></HostField>
          <HostField label={t('打开方式')}><HostSelect aria-label={t('打开方式')} value={mode} onChange={e => setMode(e.target.value as 'browser' | 'embedded')}><option value="browser">{t('浏览器')}</option><option value="embedded">{t('嵌入工作区')}</option></HostSelect></HostField>
          {mode === 'embedded' && <EmbedPolicyFields value={embedding} onChange={setEmbedding}/>}
          <HostCheckbox checked={form.enabled} onChange={e => setForm({...form,enabled:e.target.checked})}>{t('启用')}</HostCheckbox>
          <HostToolbar>
            <HostButton tone="primary" submit>{saving ? t('正在保存…') : t('保存外部应用')}</HostButton>
          </HostToolbar>
          <HostHint>{t('并发更新冲突时，请重新加载并选择最新应用后再编辑。')}</HostHint>
        </HostFieldset>
      </form>
    </HostSection>
    <ExternalAuthenticationManager key={token} token={token}/>
  </div>;
}
