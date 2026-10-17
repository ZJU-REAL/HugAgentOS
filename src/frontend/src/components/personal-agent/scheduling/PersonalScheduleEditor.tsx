import { useState } from 'react';
import { Alert, Form, Input, Modal, Select, Switch } from 'antd';
import { personalApi, errorText, type PersonalSchedule, type PersonalSchedulePatch } from '../api';
import { t } from '../../../i18n';

// Keep custom expressions intact: opening settings must never normalize a schedule into a different one.
const PRESETS = ['0 8 * * *', '0 8 * * 1-5', '0 8 * * 1', '0 * * * *'];
export function PersonalScheduleEditor({ item, onClose, onSaved }: {
  item: PersonalSchedule; onClose: () => void; onSaved: () => Promise<void>;
}) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const kind = Form.useWatch('kind', form) || (item.cron ? 'recurring' : 'once');
  const originalTime = (item.run_at || item.next_run || '').slice(0, 16);
  async function save() {
    let values;
    try { values = await form.validateFields(); } catch { return; }
    setSaving(true);setError('');
    const patch: PersonalSchedulePatch = { title: values.title.trim(), instruction: values.instruction.trim(),
      timezone: values.timezone.trim(), enabled: values.enabled, revision: item.revision ?? 0 };
    // Send timing only when edited, so a text edit does not move the next occurrence.
    if (values.kind !== (item.cron ? 'recurring' : 'once') || (values.kind === 'recurring' ? values.cron !== (item.cron || '') : values.run_at !== originalTime) || values.timezone.trim() !== item.timezone) {
      patch.cron = values.kind === 'recurring' ? values.cron.trim() : '';
      patch.run_at = values.kind === 'once' ? values.run_at : '';
    } else { delete patch.timezone; }
    try {
      await personalApi.updateSchedule(item.id, patch);
      await onSaved();onClose();
    } catch (exc) { setError(errorText(exc)); } finally { setSaving(false); }
  }
  return <Modal open styles={{ body: { maxHeight: '65vh', overflowY: 'auto', paddingRight: 8 } }} title={t('定时任务设置')} onCancel={() => { if (!saving) onClose(); }} onOk={() => void save()}
    okButtonProps={{ 'aria-label': t('保存') }} okText={t('保存')} cancelText={t('取消')} confirmLoading={saving} cancelButtonProps={{ disabled: saving }} closable={!saving} maskClosable={!saving}>
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
    <Form form={form} layout="vertical" disabled={saving} initialValues={{ title: item.title, instruction: item.instruction,
      kind: item.cron ? 'recurring' : 'once', cron: item.cron || '', run_at: originalTime, timezone: item.timezone, enabled: item.enabled !== false }}>
      <Form.Item name="title" label={t('任务名称')} rules={[{ required: true, whitespace: true }]}><Input maxLength={100} /></Form.Item>
      <Form.Item name="instruction" label={t('执行内容')} rules={[{ required: true, whitespace: true }]}><Input.TextArea rows={4} maxLength={4000} showCount /></Form.Item>
      <Form.Item name="kind" label={t('执行方式')}><Select options={[{ value: 'recurring', label: t('重复') }, { value: 'once', label: t('一次') }]} /></Form.Item>
      {kind === 'recurring' ? <>
        <Form.Item label={t('常用时间')}><Select aria-label={t('常用时间')} placeholder={t('选择常用时间')} onChange={value => form.setFieldValue('cron', value)} options={PRESETS.map((value, i) => ({ value, label: [t('每天 08:00'), t('工作日 08:00'), t('每周一 08:00'), t('每小时整点')][i] }))} /></Form.Item>
        <Form.Item name="cron" label={t('重复规则')} extra={t('五段 cron：分 时 日 月 周；最短间隔 10 分钟。')} rules={[{ required: true, whitespace: true }]}><Input aria-label={t('重复规则')} /></Form.Item>
      </> : <Form.Item name="run_at" label={t('执行时间')} rules={[{ required: true }]}><Input type="datetime-local" /></Form.Item>}
      <Form.Item name="timezone" label={t('时区')} extra={t('时间按此时区执行，例如 Asia/Shanghai。')} rules={[{ required: true, whitespace: true }]}><Input /></Form.Item>
      <Form.Item name="enabled" label={t('启用定时任务')} valuePropName="checked"><Switch /></Form.Item>
    </Form>
  </Modal>;
}
