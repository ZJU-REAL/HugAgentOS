import {ApplicationBadge,ApplicationActivityStatus} from './ApplicationBadge';
import { useRef, useState, type KeyboardEvent, type Ref } from 'react';
import type { ApplicationEntry } from '../applicationEntries';
import { t } from '../i18n';
import { ApplicationIcon } from './ApplicationIcon';
import { reloadNavigationPreferences, saveNavigationPreferences, useNavigationPreferences } from './preferences';
import { reloadDirectory, useApplicationDirectory } from './catalog';

export function ApplicationList({ entries, defaults, currentKey, onOpen, menu = false, searchRef }: {
  entries: ApplicationEntry[]; defaults: string[]; currentKey?: string;
  onOpen: (entry: ApplicationEntry) => void; menu?: boolean; searchRef?: Ref<HTMLInputElement>;
}) {
  const [query, setQuery] = useState('');
  const state = useNavigationPreferences();
  const catalog = useApplicationDirectory();
  const list = useRef<HTMLDivElement>(null);
  const pinned = state.value.pinned ?? defaults;
  const busy = state.loading || state.saving || catalog.loading || !!catalog.error;
  const filtered = entries.filter(entry =>
    [entry.title,entry.description,entry.group,entry.source].some(value => value.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())));
  function keyboard(event: KeyboardEvent) {
    if (!['ArrowDown','ArrowUp','Home','End'].includes(event.key)) return;
    const nodes = [...(list.current?.querySelectorAll<HTMLButtonElement>('[data-application-open]:not(:disabled)') || [])];
    if (!nodes.length) return;
    const index = nodes.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? nodes.length - 1 :
      event.key === 'ArrowDown' ? (index + 1) % nodes.length : (index <= 0 ? nodes.length : index) - 1;
    event.preventDefault(); nodes[next].focus();
  }
  return <section className="jx-applicationBrowser" aria-label={t('应用列表')}>
    <label className="jx-applicationSearch">{t('搜索应用')}<input type="search" ref={searchRef} aria-label={t('搜索应用')}
      value={query} onChange={event => setQuery(event.target.value)} onKeyDown={keyboard} /></label>
    {catalog.loading && <p role="status">{t('正在加载应用目录…')}</p>}
    {!!catalog.error && <p role="alert">{t(catalog.error)} <button onClick={reloadDirectory}>{t('重试')}</button></p>}
    {!!state.error && <p role="alert">{state.error} <button disabled={state.saving} onClick={() => void reloadNavigationPreferences()}>{t('重试')}</button></p>}
    <div ref={list} className="jx-applicationList" role={menu ? 'menu' : undefined} aria-label={menu ? t('应用列表') : undefined} onKeyDown={keyboard}>
      {filtered.map(entry => {
        const forced = catalog.policy.forced.includes(entry.key);
        const fixed = ['builtin:chat','builtin:settings','builtin:docs','builtin:plan_mode'].includes(entry.key);
        const selected = fixed || forced || pinned.includes(entry.key);
        const current = currentKey === entry.key;
        const details = [entry.source, entry.group, current ? t('当前应用') : ''].filter(Boolean).join(' · ');
        return <div key={entry.key} className={'jx-applicationRow' + (current ? ' active' : '')}>
          <button type="button" role={menu ? 'menuitem' : undefined} data-application-open={entry.key}
            aria-current={current ? 'page' : undefined}
            disabled={!entry.item.availability.available} onClick={() => onOpen(entry)}>
            <ApplicationIcon icon={entry.icon} builtinIcon={entry.builtinIcon} />
            <ApplicationBadge applicationKey={entry.key}/><ApplicationActivityStatus applicationKey={entry.key}/><span><strong>{entry.title}</strong><small>{entry.description}</small>
              {details && <small>{details}</small>}
              {!entry.item.availability.available && <small>{t(entry.item.availability.reason === 'auth_configuration_required' ? '请管理员配置此应用的目标认证适配' : entry.item.availability.reason === 'app_disabled' ? '应用已停用' : '暂不可用')}</small>}</span>
          </button>
          <button type="button" className="jx-applicationPin" role={menu ? 'menuitemcheckbox' : 'checkbox'} aria-label={entry.title + ' ' + t('固定')}
            aria-checked={selected} disabled={state.loading || catalog.loading || !!catalog.error || forced || fixed} aria-disabled={busy || forced || fixed} title={forced ? t('强制入口') : t('固定')}
            onClick={() => { if (!busy && !forced && !fixed) void saveNavigationPreferences(selected ? pinned.filter(key => key !== entry.key) :
              [...new Set([...pinned,entry.key])],state.value.order); }}>{selected ? '★' : '☆'}</button>
        </div>;
      })}
    </div>
    {!filtered.length && <p role="status">{t('没有匹配的应用')}</p>}
    <p role="status">{state.saving ? t('正在保存导航偏好') : ''}</p>
  </section>;
}
