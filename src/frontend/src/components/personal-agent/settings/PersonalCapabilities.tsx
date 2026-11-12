import { useEffect, useState } from 'react';
import { Alert, Button, Select, Tag, Tooltip, message } from 'antd';
import { ApiOutlined, AppstoreAddOutlined, BulbOutlined, CheckCircleOutlined, ExclamationCircleOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { type CatalogResponse } from '../../../api';
import {
  personalApi, personalRequest, errorText,
  type CapabilitySelection, type CapabilitySync, type PersonalAgent, type PersonalCapabilityOptions,
} from '../api';
import { PersonalCapabilityMarkets, type CapabilityField } from './PersonalCapabilityMarkets';
import { SettingRow } from './SettingRow';
import { formatDateTime } from '../../../utils/date';
import { t, tCtx } from '../../../i18n';
import './personal-capabilities.css';

interface PersonalCapabilitiesProps {
  agent: PersonalAgent;
  onChange: () => Promise<void>;
}

const EMPTY: CapabilitySelection = { skill_ids: [], mcp_ids: [], plugin_ids: [] };
const FIELDS: CapabilityField[] = ['skill_ids', 'mcp_ids', 'plugin_ids'];

const sameSelection = (a: CapabilitySelection, b: CapabilitySelection) =>
  FIELDS.every(key => a[key].length === b[key].length && a[key].every((id, index) => id === b[key][index]));

/** What the server kept: its saved selection and the last copy into the agent's workspace. */
function savedState(agent: PersonalAgent) {
  return { selection: agent.config.capability_selection || EMPTY, sync: agent.config.capability_sync };
}

/**
 * Owner choices are saved separately from the account's current grants. Saving (and importing
 * from a market) copies the bound skills and plugins into the agent's workspace on the server.
 */
export function PersonalCapabilities({ agent, onChange }: PersonalCapabilitiesProps) {
  const [catalog, setCatalog] = useState<CatalogResponse>();
  const [available, setAvailable] = useState<PersonalCapabilityOptions>();
  const [saved, setSaved] = useState(() => savedState(agent));
  // undefined: no unsaved change, the page shows what the server kept.
  const [draft, setDraft] = useState<CapabilitySelection>();
  const [saving, setSaving] = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState('');
  const [refreshing, setRefreshing] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const agentName = agent.config.name || 'Hugo';

  // A refreshed view from the parent replaces what this page last heard from the server.
  const { capability_selection: viewSelection, capability_sync: viewSync } = agent.config;
  useEffect(() => { setSaved({ selection: viewSelection || EMPTY, sync: viewSync }); }, [viewSelection, viewSync]);

  useEffect(() => {
    const controller = new AbortController();
    setRefreshing(true);
    void Promise.all([
      personalApi.capabilities(controller.signal),
      personalRequest<CatalogResponse>('/v1/catalog', { signal: controller.signal }).catch(() => undefined),
    ]).then(([options, names]) => {
      if (controller.signal.aborted) return;
      setAvailable(options);
      setCatalog(names);
      setError('');
    }).catch(exc => { if (!controller.signal.aborted) setError(errorText(exc)); })
      .finally(() => { if (!controller.signal.aborted) setRefreshing(false); });
    return () => controller.abort();
  }, [attempt]);

  const current = draft || saved.selection;
  const dirty = draft !== undefined && !sameSelection(draft, saved.selection);
  const busy = saving || importing || refreshing;

  async function persist(selection: CapabilitySelection) {
    const updated = await personalApi.settings(selection);
    const next = savedState(updated);
    setSaved(next);
    void onChange();
    return next;
  }

  async function save() {
    setSaving(true);
    setError('');
    try {
      const next = await persist(current);
      setDraft(undefined);
      if (next.sync?.status === 'failed') message.error(t('能力绑定已保存，但没有同步到 {name} 的工作区。', { name: agentName }));
      else message.success(t('能力绑定已保存，下次执行任务时生效。'));
    } catch (exc) { setError(errorText(exc)); }
    finally { setSaving(false); }
  }

  /** A market import binds the item at once, so it is copied into the workspace like a save. */
  async function imported(field: CapabilityField, id?: string) {
    setImporting(true);
    setError('');
    try {
      const options = await personalApi.capabilities();
      setAvailable(options);
      const allowed = field === 'plugin_ids' ? options.plugins.map(item => item.id) : options[field];
      if (!id || !allowed.includes(id)) {
        message.info(t('已导入账号；当前不可绑定的项目需先在能力中心启用，插件内部能力请通过插件绑定。'));
        return;
      }
      const add = (ids: string[]) => Array.from(new Set([...ids, id]));
      const next = await persist({ ...saved.selection, [field]: add(saved.selection[field]) });
      // Unsaved edits stay a draft; the imported item joins them.
      setDraft(value => value && { ...value, [field]: add(value[field]) });
      if (next.sync?.status === 'failed') message.error(t('已导入并绑定，但没有同步到 {name} 的工作区。', { name: agentName }));
      else message.success(t('已导入并绑定到 {name}。', { name: agentName }));
    } catch (exc) { setError(errorText(exc)); }
    finally { setImporting(false); }
  }

  const options = (ids: string[], names?: { id: string; name: string }[]) =>
    ids.map(id => ({ value: id, label: names?.find(item => item.id === id)?.name || id }));
  const select = (key: CapabilityField, items: { value: string; label: string }[]) =>
    <Select mode="multiple" aria-label={t('绑定') + ' ' + t(key === 'skill_ids' ? '技能' : key === 'mcp_ids' ? '工具' : '插件')}
      style={{ width: '100%' }} allowClear showSearch optionFilterProp="label" value={available ? current[key] : []}
      options={[...items, ...(available ? current[key] : []).filter(id => !items.some(item => item.value === id))
        .map(id => ({ value: id, label: id + ' ' + t('（暂不可用）') }))]} loading={!available && !error} disabled={!available || busy}
      placeholder={t('选择要绑定的能力')}
      onChange={(value: string[]) => setDraft({ ...current, [key]: value })} />;

  const market = (field: CapabilityField) => <PersonalCapabilityMarkets field={field} disabled={busy || !available}
    onImported={(importField, id) => void imported(importField, id)} />;

  const builtins = available?.builtin_skills || [];
  const skillName = (id: string) => catalog?.skills?.find(item => item.id === id)?.name || id;

  return <>
    {error && <Alert type="error" showIcon title={error} action={<Button onClick={() => setAttempt(value => value + 1)}>{t('重试')}</Button>} />}
    <SettingRow stacked icon={<ApiOutlined />} label={t('工具')}
      action={market('mcp_ids')}
      control={select('mcp_ids', options(available?.mcp_ids || [], catalog?.mcp))} />
    <SettingRow stacked icon={<BulbOutlined />} label={t('技能')}
      action={market('skill_ids')}
      control={<div className="pa-cap-skills">
        {builtins.length > 0 && <div className="pa-cap-builtin" role="list" aria-label={t('{name} 自带的技能', { name: agentName })}>
          {builtins.map(skill => <Tooltip key={skill.id} title={skill.description || undefined}>
            <Tag role="listitem" className="pa-cap-builtin-chip" variant="filled">
              <span>{skill.name}</span><span className="pa-cap-builtin-badge">{tCtx('personalAgent', '自带')}</span>
            </Tag>
          </Tooltip>)}
        </div>}
        {select('skill_ids', options(available?.skill_ids || [], catalog?.skills))}
      </div>} />
    <SettingRow stacked icon={<AppstoreAddOutlined />} label={t('插件')}
      description={t('插件在任务需要时才加载。')}
      action={market('plugin_ids')}
      control={select('plugin_ids', options((available?.plugins || []).map(plugin => plugin.id), available?.plugins))} />
    <div className="pa-cap-footer">
      <SyncState sync={saved.sync} name={agentName} skillName={skillName} />
      <div className="pa-cap-actions">
        {saved.sync?.status === 'failed' && !dirty && <Button loading={saving} disabled={busy || !available} onClick={() => void save()}>{t('重新同步')}</Button>}
        <Button disabled={busy || !dirty} onClick={() => { setDraft(undefined); setError(''); }}>{t('取消修改')}</Button>
        <Button type="primary" loading={saving} disabled={!available || !dirty || busy} onClick={() => void save()}>{t('保存能力绑定')}</Button>
      </div>
    </div>
  </>;
}

interface SyncStateProps {
  sync?: CapabilitySync;
  name: string;
  skillName: (id: string) => string;
}

/** When the bound skills and plugins were last copied into the agent's workspace, and what was left out. */
function SyncState({ sync, name, skillName }: SyncStateProps) {
  if (!sync) return <p className="pa-cap-sync">{t('保存后，绑定的技能和插件会同步到 {name} 的工作区。', { name })}</p>;
  const time = formatDateTime(sync.synced_at * 1000);
  const skipped = (sync.skipped || []).filter(item => item.reason === 'name_taken');
  return <div className="pa-cap-sync" role="status">
    {sync.status === 'failed'
      ? <p className="pa-cap-sync-line pa-cap-sync-line--failed"><ExclamationCircleOutlined aria-hidden="true" />
        <span>{t('{time} 同步失败：{reason}。可点“重新同步”再试。', { time, reason: sync.error || t('未知原因') })}</span></p>
      : <p className="pa-cap-sync-line"><CheckCircleOutlined aria-hidden="true" className="pa-cap-sync-ok" />
        <span>{t('已同步到 {name} 的工作区 · {time}', { name, time })}</span></p>}
    {skipped.length > 0 && <p className="pa-cap-sync-line"><InfoCircleOutlined aria-hidden="true" />
      <span>{t('{name} 已有同名技能，保留 {name} 自己的版本：{skills}', { name, skills: skipped.map(item => skillName(item.id)).join('、') })}</span></p>}
  </div>;
}
