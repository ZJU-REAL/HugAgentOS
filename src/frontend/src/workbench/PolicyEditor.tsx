import { useCallback, useEffect, useRef, useState } from 'react';
import { configFetch } from '../utils/adminApi';
import { applicationText, externalErrorMessage } from './externalApi';
import { reloadDirectory, type CatalogItem, type LayoutPolicy } from './catalog';
import { t } from '../i18n';
import { HostButton, HostCheckbox, HostMeta, HostNotice, HostSection, HostToolbar } from './ui';

const LOCKED = ['builtin:chat','builtin:settings','builtin:docs','builtin:plan_mode'];
const FIELDS = {defaultPinned:'默认入口', forced:'强制入口', hidden:'隐藏入口'} as const;

export function PolicyEditor({token}: {token: string}) {
  const [policy, setPolicy] = useState<LayoutPolicy & {hidden: string[]} | null>(null);
  const [items, setItems] = useState<CatalogItem[]>([]);
  const [feedback, setFeedback] = useState<{tone: 'success' | 'error'; text: string} | null>(null);
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const request = useRef<AbortController | null>(null);
  const owner = useRef<string | null>(null);
  const generation = useRef(0);
  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    const version = ++generation.current; owner.current = token;
    setBusy(true); setPolicy(null); setFeedback(null);
    try {
      const [layout, catalog] = await Promise.all([
        configFetch(token, '/v1/workbench/admin/policy', {signal:controller.signal}),
        configFetch(token, '/v1/workbench/admin/catalog', {signal:controller.signal}),
      ]);
      if (controller.signal.aborted || version !== generation.current) return;
      setPolicy(layout.data); setItems(catalog.data.items); setConflict(false);
    } catch (error) {
      if (!controller.signal.aborted && version === generation.current) setFeedback({tone: 'error', text: externalErrorMessage(error)});
    } finally { if (version === generation.current) setBusy(false); }
  }, [token]);
  const cancelRequest = useCallback(() => {
    request.current?.abort(); generation.current += 1; owner.current = null;
  }, []);
  useEffect(() => {
    void load();
    return cancelRequest;
  }, [load, cancelRequest]);
  function toggle(field: 'defaultPinned' | 'forced' | 'hidden', key: string, checked: boolean) {
    if (!policy) return;
    const next = {...policy, [field]: checked ? [...policy[field], key] : policy[field].filter(k => k !== key)};
    if (checked && field === 'hidden') {
      next.defaultPinned = next.defaultPinned.filter(k => k !== key);
      next.forced = next.forced.filter(k => k !== key);
    } else if (checked) next.hidden = next.hidden.filter(k => k !== key);
    setPolicy(next);
  }
  async function save() {
    if (!policy || owner.current !== token) return;
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    const version = ++generation.current;
    setBusy(true); setFeedback(null);
    try {
      const result = await configFetch(token, '/v1/workbench/admin/policy', {
        method: 'PUT', body: JSON.stringify(policy), signal:controller.signal,
      });
      if (controller.signal.aborted || version !== generation.current) return;
      setPolicy(result.data); reloadDirectory(); setFeedback({tone: 'success', text: t('工作台策略已保存')});
    } catch (error) {
      if (controller.signal.aborted || version !== generation.current) return;
      setFeedback({tone: 'error', text: externalErrorMessage(error)});
      // Preserve the draft. A conflict requires an explicit reload before retry.
      setConflict(true);
    } finally { if (version === generation.current) setBusy(false); }
  }
  return <HostSection title={t('工作台布局策略')}
    description={t('布局不授予应用权限。强制入口仍受用户权限和运行条件限制。')}
    actions={<HostButton tone="text" disabled={busy} onClick={() => void load()}>{t('重新加载策略')}</HostButton>}>
    {feedback && <HostNotice tone={feedback.tone}>{feedback.text}</HostNotice>}
    {!policy && busy && <HostNotice tone="loading">{t('正在加载应用目录…')}</HostNotice>}
    {policy && owner.current === token && <>
      <HostMeta>revision {policy.revision}</HostMeta>
      <ol className="jx-hostList">{items.map(item => <li key={item.applicationKey} className="jx-hostList-row">
        <span className="jx-hostList-title">{applicationText(item.declaration.title)} <HostMeta>{item.applicationKey}</HostMeta></span>
        {(Object.keys(FIELDS) as (keyof typeof FIELDS)[]).map(field => <HostCheckbox key={field}
          disabled={busy || conflict || LOCKED.includes(item.applicationKey)}
          aria-label={item.applicationKey + ' ' + field}
          checked={policy[field].includes(item.applicationKey)}
          onChange={e => toggle(field, item.applicationKey, e.target.checked)}>{t(FIELDS[field])}</HostCheckbox>)}
      </li>)}</ol>
      <ol aria-label={t('默认入口顺序')} className="jx-hostList">{policy.defaultPinned.map((key, index) => <li key={key} className="jx-hostList-row">
        <span className="jx-hostList-title"><code>{key}</code></span>
        <HostButton tone="text" size="small" disabled={busy || conflict || index === 0} onClick={() => {
          const next = [...policy.defaultPinned];
          [next[index - 1], next[index]] = [next[index], next[index - 1]];
          setPolicy({...policy, defaultPinned: next});
        }}>{t('上移默认入口')}</HostButton>
      </li>)}</ol>
      <HostToolbar>
        <HostButton tone="primary" disabled={busy || conflict} onClick={() => void save()}>{t('保存工作台策略')}</HostButton>
      </HostToolbar>
    </>}
  </HostSection>;
}
