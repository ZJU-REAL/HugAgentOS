import { Button, Checkbox } from 'antd';
import { ArrowDownOutlined, ArrowUpOutlined } from '@ant-design/icons';
import { t } from '../i18n';
import './navigation.css';
import { useApplicationDirectory } from './catalog';
import { applicationText } from './externalApi';
import {sourceLabel} from './sourceIdentity';
import {SourceStatus} from './SourceStatus';
import { orderedNavigation, useNavigationPreferences, type NavigationPreferences } from './preferences';

export function NavigationManager({ defaults, value, onChange, disabled = false }: {
  defaults: string[];
  value: NavigationPreferences;
  onChange: (value: NavigationPreferences) => void;
  disabled?: boolean;
}) {
  const catalog = useApplicationDirectory();
  const state = useNavigationPreferences();
  const pinned = value.pinned ?? defaults;
  const selected = orderedNavigation(defaults, catalog.policy.forced, value);
  const busy = disabled || state.loading || state.saving || catalog.loading || !!catalog.error;
  const fixed = new Set(['builtin:chat', 'builtin:settings', 'builtin:docs', 'builtin:plan_mode']);
  const items = catalog.items.filter(item => !fixed.has(item.applicationKey)).sort((a, b) =>
    (selected.includes(a.applicationKey) ? selected.indexOf(a.applicationKey) : selected.length) -
    (selected.includes(b.applicationKey) ? selected.indexOf(b.applicationKey) : selected.length));
  const move = (key: string, direction: number) => {
    const index = selected.indexOf(key); const target = index + direction;
    if (index < 0 || target < 0 || target >= selected.length || busy) return;
    const next = [...selected]; [next[index], next[target]] = [next[target], next[index]];
    // Unavailable references stay at their existing positions in the persisted order.
    const retained = value.order.filter(value => !selected.includes(value));
    onChange({ ...value, pinned, order: [...next, ...retained] });
  };
  return <section className="jx-navigationSettings" aria-label={t('导航偏好')}>
    <SourceStatus failures={catalog.sourceErrors} />
    {state.loading && <p role="status">{t('正在加载导航偏好')}</p>}
    {!!(state.error || catalog.error) && <p role="alert">{state.error || t(catalog.error)}</p>}
    <div className="jx-navigationSettings-toolbar">
      <Button disabled={busy} onClick={() => onChange({ ...value, pinned: null, order: [] })}>{t('恢复默认')}</Button>
    </div>
    <div className="jx-navigationSettings-list">
      {items.map(item => {
        const key = item.applicationKey;
        const forced = catalog.policy.forced.includes(key);
        const checked = forced || pinned.includes(key);
        const title = applicationText(item.declaration.title);
        const source = t(sourceLabel(item.origin));
        const label = [title, source].filter(Boolean).join(' ');
        return <div key={key} className="jx-navigationSettings-item" onKeyDown={event => {
          if (event.altKey && ['ArrowUp', 'ArrowDown'].includes(event.key)) {
            event.preventDefault();
            move(key, event.key === 'ArrowUp' ? -1 : 1);
          }
        }}>
          <Checkbox aria-label={label + ' ' + t('固定')} checked={checked} disabled={busy || forced}
            onChange={event => {
              const next = event.target.checked ? [...pinned, key] : pinned.filter(value => value !== key);
              onChange({ ...value, pinned: [...new Set(next)] });
            }}>
            {title}{source && ' · ' + source}{forced && ' · ' + t('强制入口')}
            {!item.availability.available && ' · ' + t('暂不可用')}
          </Checkbox>
          {checked && <div className="jx-navigationSettings-order">
            <Button size="small" aria-label={label + ' ' + t('上移')} icon={<ArrowUpOutlined />}
              disabled={busy || selected.indexOf(key) <= 0} onClick={() => move(key, -1)} />
            <Button size="small" aria-label={label + ' ' + t('下移')} icon={<ArrowDownOutlined />}
              disabled={busy || selected.indexOf(key) === selected.length - 1} onClick={() => move(key, 1)} />
          </div>}
        </div>;
      })}
    </div>
  </section>;
}
