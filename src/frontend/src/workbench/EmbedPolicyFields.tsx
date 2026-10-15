import {t} from '../i18n';
import type {EmbeddingPolicy} from './externalEmbed';

export function EmbedPolicyFields({value, onChange}: {
  value: EmbeddingPolicy; onChange: (value: EmbeddingPolicy) => void;
}) {
  return <fieldset>
    <legend>{t('嵌入安全配置')}</legend>
    <label>{t('精确外部 origin')}<input type="url" required maxLength={2048}
      value={value.origin} onChange={e => onChange({...value, origin:e.target.value})}/></label>
    <label>{t('就绪确认')}<select aria-label={t('就绪确认')} value={value.ready} onChange={e => onChange({
      ...value, ready:e.target.value as EmbeddingPolicy['ready'],
    })}><option value="unconfirmed">{t('普通页面：不声明已就绪')}</option>
      <option value="integrated">{t('集成页面：验证 ready 握手')}</option></select></label>
    <label><input type="checkbox" checked={value.originStorage}
      onChange={e => onChange({...value, originStorage:e.target.checked})}/>{t('信任此独立 origin，允许其自身存储（集成握手必需）')}</label>
    <label><input type="checkbox" checked={value.allowForms}
      onChange={e => onChange({...value, allowForms:e.target.checked})}/>{t('允许外部页面提交表单')}</label>
    <p>{t('弹窗、下载、顶层导航和设备权限保持禁用。不会绕过对方的嵌入限制。')}</p>
  </fieldset>;
}
