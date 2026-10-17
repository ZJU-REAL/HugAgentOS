import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Select, Space, message } from 'antd';
import { ApiOutlined, AppstoreAddOutlined, BulbOutlined } from '@ant-design/icons';
import { type CatalogResponse } from '../../../api';
import { personalApi, personalRequest, errorText, type PersonalAgent, type PersonalCapabilityOptions } from '../api';
import { PersonalCapabilityMarkets, type CapabilityField } from './PersonalCapabilityMarkets';
import { SettingRow } from './SettingRow';
import { t } from '../../../i18n';

interface PersonalCapabilitiesProps {
  agent: PersonalAgent;
  onChange: () => Promise<void>;
}

function bindings(agent: PersonalAgent) {
  return agent.config.capability_selection || {
    skill_ids: agent.config.skill_ids || [],
    mcp_ids: agent.config.mcp_ids || [],
    plugin_ids: (agent.config.plugins || []).map(plugin => plugin.id),
  };
}

/** Owner choices are saved separately from the account's current grants. */
export function PersonalCapabilities({ agent, onChange }: PersonalCapabilitiesProps) {
  const [catalog, setCatalog] = useState<CatalogResponse>();
  const [available, setAvailable] = useState<PersonalCapabilityOptions>();
  const [draft, setDraft] = useState(() => bindings(agent));
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const pendingImports = useRef<{ field: CapabilityField; id?: string }[]>([]);
  const [refreshing, setRefreshing] = useState(true);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setRefreshing(true);
    void Promise.all([
      personalApi.capabilities(controller.signal),
      personalRequest<CatalogResponse>('/v1/catalog', { signal: controller.signal }).catch(() => undefined),
    ]).then(([options, names]) => {
      if (controller.signal.aborted) return;
      setAvailable(options);
      const imports = pendingImports.current.splice(0);
      setDraft(current => {
        const next = { ...current,
          skill_ids: current.skill_ids.filter(id => !options.plugin_component_ids?.skill_ids.includes(id)),
          mcp_ids: current.mcp_ids.filter(id => !options.plugin_component_ids?.mcp_ids.includes(id)),
        };
        for (const { field, id } of imports) {
          const allowed = field === 'plugin_ids' ? options.plugins.map(item => item.id) : options[field];
          if (id && allowed.includes(id)) next[field] = Array.from(new Set([...next[field], id]));
        }
        return next;
      });
      if (imports.length) {
        setDirty(true);
        if (imports.some(({ field, id }) => !id || !(field === 'plugin_ids' ? options.plugins.map(item => item.id) : options[field]).includes(id))) {
          message.info(t('已导入账号；当前不可绑定的项目需先在能力中心启用，插件内部能力请通过插件绑定。'));
        }
      }
      setCatalog(names);
      setError('');
    }).catch(exc => { if (!controller.signal.aborted) setError(errorText(exc)); }).finally(() => { if (!controller.signal.aborted) setRefreshing(false); });
    return () => controller.abort();
  }, [attempt]);

  async function save() {
    setSaving(true);
    setError('');
    try {
      const updated = await personalApi.settings(draft);
      setDraft(bindings(updated));
      setDirty(false);
      message.success(t('能力绑定已保存，下次执行任务时生效。'));
      await onChange();
    } catch (exc) { setError(errorText(exc)); }
    finally { setSaving(false); }
  }

  const options = (ids: string[], names?: { id: string; name: string }[]) =>
    ids.map(id => ({ value: id, label: names?.find(item => item.id === id)?.name || id }));
  const select = (key: keyof typeof draft, items: { value: string; label: string }[]) =>
    <Select mode="multiple" aria-label={t('绑定') + ' ' + t(key === 'skill_ids' ? '技能' : key === 'mcp_ids' ? '工具' : '插件')}
      style={{ width: '100%' }} allowClear showSearch optionFilterProp="label" value={available ? draft[key] : []}
      options={[...items, ...(available ? draft[key] : []).filter(id => !items.some(item => item.value === id))
        .map(id => ({ value: id, label: id + ' ' + t('（暂不可用）') }))]} loading={!available && !error} disabled={!available || saving || refreshing}
      placeholder={t('选择要绑定的能力')}
      onChange={(value: string[]) => { setDraft(current => ({ ...current, [key]: value })); setDirty(true); }} />;

  const market = (field: CapabilityField) => <PersonalCapabilityMarkets field={field} disabled={saving || refreshing || !available}
    onImported={(importField, id) => {
      pendingImports.current.push({ field: importField, id });
      setAttempt(value => value + 1);
    }} />;

  return <>
    {error && <Alert type="error" showIcon title={error} action={<Button onClick={() => setAttempt(value => value + 1)}>{t('重试')}</Button>} />}
    <SettingRow stacked icon={<ApiOutlined />} label={t('工具')}
      action={market('mcp_ids')}
      control={select('mcp_ids', options(available?.mcp_ids || [], catalog?.mcp))} />
    <SettingRow stacked icon={<BulbOutlined />} label={t('技能')}
      action={market('skill_ids')}
      control={select('skill_ids', options(available?.skill_ids || [], catalog?.skills))} />
    <SettingRow stacked icon={<AppstoreAddOutlined />} label={t('插件')}
      action={market('plugin_ids')}
      control={select('plugin_ids', options((available?.plugins || []).map(plugin => plugin.id), available?.plugins))} />
    <Space style={{ marginTop: 12 }}>
      <Button type="primary" loading={saving} disabled={!available || !dirty || refreshing || pendingImports.current.length > 0} onClick={() => void save()}>{t('保存能力绑定')}</Button>
      <Button disabled={saving || !dirty || refreshing || pendingImports.current.length > 0} onClick={() => { setDraft({ ...bindings(agent),
        skill_ids: bindings(agent).skill_ids.filter(id => !available?.plugin_component_ids?.skill_ids.includes(id)),
        mcp_ids: bindings(agent).mcp_ids.filter(id => !available?.plugin_component_ids?.mcp_ids.includes(id)),
      }); setDirty(false); setError(''); }}>{t('取消修改')}</Button>
    </Space>
  </>;
}
