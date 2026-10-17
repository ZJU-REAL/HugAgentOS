import { useEffect, useMemo, useState } from 'react';
import { Modal, Select, Tag, message } from 'antd';
import {
  ClockCircleOutlined, FolderOpenOutlined, MailOutlined,
  RightOutlined, RobotOutlined, ThunderboltOutlined,
} from '@ant-design/icons';
import { useModelCapabilitiesStore } from '../../../stores';
import { EmailConnect } from '../../settings/EmailConnect';
import { PersonalMemory } from '../workspace/PersonalMemory';
import { PersonalAvatar } from '../shared/PersonalAvatar';
import { PersonalCapabilities } from './PersonalCapabilities';
import { PersonalHeartbeat } from '../scheduling/PersonalHeartbeat';
import { SettingGroup, SettingRow } from './SettingRow';
import { personalApi, errorText, type PersonalMonitor } from '../api';
import { DEFAULT_CONCURRENCY, MAX_CONCURRENCY } from '../shared/limits';
import { t } from '../../../i18n';
import { effortLabels, normalizeThinkingEffort, selectedReasoningPolicy, thinkingLevels } from '../../../utils/reasoningEffort';

interface PersonalSettingsProps {
  open: boolean;
  monitor?: PersonalMonitor;
  name: string;
  onClose: () => void;
  onProfile: () => void;
  onChange: () => Promise<void>;
}

function timezones(current?: string) {
  const all = typeof Intl.supportedValuesOf === 'function' ? Intl.supportedValuesOf('timeZone') : [];
  return [...new Set([current, ...all].filter(Boolean) as string[])].map(value => ({ value, label: value.replace(/_/g, ' ') }));
}

/** Personal Agent settings, grouped by what the user is deciding. */
export function PersonalSettings({ open, monitor, name, onClose, onProfile, onChange }: PersonalSettingsProps) {
  const agent = monitor?.agent;
  const zones = useMemo(() => timezones(agent?.config.timezone), [agent?.config.timezone]);
  const [saving, setSaving] = useState(false);
  const models = useModelCapabilitiesStore(s => s.capabilities);
  useEffect(() => {
    if (!open) return;
    if (!useModelCapabilitiesStore.getState().loaded) void useModelCapabilitiesStore.getState().fetchCapabilities();
  }, [open]);
  async function save(patch: Record<string, unknown>) {
    setSaving(true);
    try { await personalApi.settings(patch); await onChange(); }
    catch (exc) { message.error(errorText(exc)); }
    finally { setSaving(false); }
  }
  const modelOptions = models.user_model_switch_enabled
    ? [{ value: '', label: t('跟随系统默认') }, ...models.user_selectable_models.map(item => ({ value: item.provider_id, label: item.display_name || item.model_name }))]
    : [];
  const policy = selectedReasoningPolicy(models, agent?.config.model_provider_id || null);
  const effort = normalizeThinkingEffort(agent?.config.thinking_effort || 'fast', policy);
  const effortOptions = [{ value: 'fast', label: t('快速') }, ...(policy.supports_reasoning_effort
    ? thinkingLevels(policy).map(({ key }) => ({ value: key, label: t(effortLabels[key]) }))
    : [{ value: 'medium', label: t('思考模式') }])];

  return <Modal open={open} title={t('{name} 设置', { name })} footer={null} onCancel={onClose} width={600} className="pa-settings-modal" destroyOnHidden>
    {agent && <div className="pa-settings">
      <button type="button" className="pa-set-hero" onClick={onProfile}>
        <PersonalAvatar avatar={agent.config.avatar} name={name} size={52} />
        <span className="pa-set-text"><strong>{name}</strong><span>{t('修改头像与名称')}</span></span>
        <RightOutlined />
      </button>

      <SettingGroup title={t('能力')}>
        <PersonalCapabilities agent={agent} onChange={onChange} />
      </SettingGroup>

      <SettingGroup title={t('连接器')}>
        <SettingRow icon={<FolderOpenOutlined />} label={t('我的空间')} description={t('可以搜索和读取“我的空间”中的文件，并把结果保存回去。')}
          control={<Tag bordered={false} color="green">{t('已接入')}</Tag>} />
        <SettingRow stacked icon={<MailOutlined />} label={t('邮箱')}
          description={t('收信和读信直接执行；发送前是否需要你批准，由输入框里的操作权限决定。凭据只保存在服务端。')}
          control={<EmailConnect compact target="cloud" onChange={() => void onChange()} />} />
      </SettingGroup>

      <SettingGroup title={t('运行')}>
        <SettingRow stacked icon={<RobotOutlined />} label={t('模型')}
          control={<div className="pa-set-model-options">
            <Select aria-label={t('模型')} value={agent.config.model_provider_id || ''}
              disabled={saving || modelOptions.length < 2} options={modelOptions.length ? modelOptions : [{ value: '', label: t('跟随系统默认') }]}
              onChange={value => void save({ model_provider_id: value,
                thinking_effort: normalizeThinkingEffort(agent.config.thinking_effort || 'fast', selectedReasoningPolicy(models, value || null)) })} />
            <label className="pa-set-effort"><span>{t('思考程度')}</span>
              <Select aria-label={t('思考程度')} disabled={saving} value={effort} options={effortOptions}
                onChange={value => void save({ thinking_effort: value })} />
            </label>
          </div>} />
        <SettingRow icon={<ThunderboltOutlined />} label={t('同时执行的任务数')} description={t('超出的任务排队等待。')}
          control={<Select aria-label={t('同时执行的任务数')} style={{ width: 88 }} value={agent.config.concurrency || DEFAULT_CONCURRENCY}
            options={Array.from({ length: MAX_CONCURRENCY }, (_, index) => ({ value: index + 1, label: String(index + 1) }))} onChange={value => void save({ concurrency: value })} />} />
        <SettingRow icon={<ClockCircleOutlined />} label={t('时区')} description={t('定时任务和回复里的时间按这个时区计算。')}
          control={<Select aria-label={t('时区')} showSearch style={{ width: 200 }} value={agent.config.timezone} options={zones} optionFilterProp="label"
            onChange={value => void save({ timezone: value })} />} />
      </SettingGroup>

      {monitor?.heartbeat && <SettingGroup title={t('心跳')}>
        <PersonalHeartbeat heartbeat={monitor.heartbeat} name={name} save={save} onChange={onChange} />
      </SettingGroup>}

      <PersonalMemory onClose={onClose} status={monitor?.file_memory} />
    </div>}
  </Modal>;
}
