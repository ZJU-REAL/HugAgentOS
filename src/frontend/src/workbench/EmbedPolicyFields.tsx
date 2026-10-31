import {t} from '../i18n';
import type {EmbeddingPolicy} from './externalEmbed';
import {HostCheckbox, HostField, HostFieldset, HostHint, HostInput, HostSelect} from './ui';

export function EmbedPolicyFields({value, onChange}: {
  value: EmbeddingPolicy; onChange: (value: EmbeddingPolicy) => void;
}) {
  return <HostFieldset legend={t('嵌入安全配置')}>
    <HostField label={t('精确外部 origin')}><HostInput type="url" required maxLength={2048}
      value={value.origin} onChange={e => onChange({...value, origin:e.target.value})}/></HostField>
    <HostField label={t('就绪确认')}><HostSelect aria-label={t('就绪确认')} value={value.ready} onChange={e => onChange({
      ...value, ready:e.target.value as EmbeddingPolicy['ready'],
    })}><option value="unconfirmed">{t('普通页面：不声明已就绪')}</option>
      <option value="integrated">{t('集成页面：验证 ready 握手')}</option></HostSelect></HostField>
    <HostCheckbox checked={value.originStorage}
      onChange={e => onChange({...value, originStorage:e.target.checked})}>{t('信任此独立 origin，允许其自身存储（集成握手必需）')}</HostCheckbox>
    <HostCheckbox checked={value.allowForms}
      onChange={e => onChange({...value, allowForms:e.target.checked})}>{t('允许外部页面提交表单')}</HostCheckbox>
    <HostHint>{t('弹窗、下载、顶层导航和设备权限保持禁用。不会绕过对方的嵌入限制。')}</HostHint>
  </HostFieldset>;
}
