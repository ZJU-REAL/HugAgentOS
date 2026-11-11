import { useEffect, useState } from 'react';
import { Button, Input, Modal, Select, message } from 'antd';
import { EditOutlined, ExclamationCircleOutlined, FieldTimeOutlined, HeartOutlined, MessageOutlined, SendOutlined } from '@ant-design/icons';
import { SettingRow } from '../settings/SettingRow';
import { personalApi, errorText, type HeartbeatOutcome, type HeartbeatUnavailable, type PersonalChannel, type PersonalHeartbeat as Heartbeat } from '../api';
import { formatShortDateTime } from '../../../utils/date';
import { t } from '../../../i18n';

const CHECKLIST = '/workspace/HEARTBEAT.md';
/** channel_id that keeps proactive results in the web conversation only. An empty channel_id means automatic. */
const WEB_ONLY = 'none';
const TIMES = Array.from({ length: 48 }, (_, index) => {
  const value = String(Math.floor(index / 2)).padStart(2, '0') + ':' + (index % 2 ? '30' : '00');
  return { value, label: value };
});
const OUTCOMES: Record<HeartbeatOutcome, string> = {
  quiet: '没有需要提醒的事',
  alert: '发送了提醒',
  delegated: '安排了一个后台任务去处理',
  duplicate: '与之前的提醒相同，未重复发送',
  failed: '主动服务失败，下次会再试',
  stale: '等待过久，已跳过',
  skipped: '已跳过',
  reported: '发送了汇总',
  nothing: '没有需要告诉你的事',
  task_failed: '处理失败，下次会再试',
};
const SKIPS: Record<string, string> = {
  empty_checklist: '清单为空，未运行',
  proactive_running: '上一次主动服务还没结束',
  paused: '智能体已暂停',
  busy: '正在回复消息，稍后再做',
  daily_limit: '已达今日上限',
  no_permission: '没有使用权限',
};
const UNAVAILABLE: Record<HeartbeatUnavailable, string> = {
  email: '邮箱未连接，邮箱相关的事项暂时不会处理',
  channel: '没有可用的私聊，结果只会显示在网页',
};
const UNAVAILABLE_ORDER: HeartbeatUnavailable[] = ['email', 'channel'];

function lastLine(heartbeat: Heartbeat) {
  const last = heartbeat.last;
  if (!last) return t('还没有运行过。');
  const what = last.outcome === 'skipped' && last.reason ? t(SKIPS[last.reason] || OUTCOMES.skipped) : t(OUTCOMES[last.outcome] || OUTCOMES.skipped);
  return t('上次 {time}：{what}', { time: formatShortDateTime(last.at), what });
}

/** Every half hour except the other run's time: the morning and evening runs cannot be the same. */
function timesExcept(taken: string) {
  return TIMES.map(option => ({ ...option, disabled: option.value === taken }));
}

interface PersonalHeartbeatProps {
  heartbeat: Heartbeat;
  name: string;
  save: (patch: Record<string, unknown>) => Promise<void>;
  onChange: () => Promise<void>;
}

/** Proactive service settings: the agent goes through HEARTBEAT.md each morning, evening and idle spell, and is quiet unless there is something to tell. */
export function PersonalHeartbeat({ heartbeat, name, save, onChange }: PersonalHeartbeatProps) {
  const [bots, setBots] = useState<PersonalChannel[]>([]);
  const [editing, setEditing] = useState(false);
  const [running, setRunning] = useState(false);
  useEffect(() => { personalApi.channels().then(items => setBots(items.filter(bot => bot.personal_agent))).catch(() => setBots([])); }, []);
  const beat = (patch: Record<string, unknown>) => save({ heartbeat: patch });
  const hours = heartbeat.active_hours;
  // "Web only" is the owner's choice, not something missing: the backend's answer can lag the choice by half a minute.
  const unavailable = UNAVAILABLE_ORDER.filter(key => heartbeat.unavailable?.includes(key) && !(key === 'channel' && heartbeat.channel_id === WEB_ONLY));
  const reduced = (heartbeat.proactive?.backoff ?? 0) > 0;

  async function runNow() {
    setRunning(true);
    try { await personalApi.runHeartbeat(); message.success(t('{name} 正在按清单把你的事过一遍。有需要告诉你的事才会发消息。', { name })); await onChange(); }
    catch (exc) { message.error(errorText(exc)); }
    finally { setRunning(false); }
  }

  return <>
    <SettingRow icon={<HeartOutlined />} label={t('主动服务')}
      description={<>{t('每天早上、晚上和你空闲时，我会按清单把你的事过一遍。没事不打扰。想停掉某一类，从清单里删掉；清空清单就不再主动服务。')}<br />{lastLine(heartbeat)}
        {heartbeat.next_run && <> · {t('下次 {time}', { time: formatShortDateTime(heartbeat.next_run) })}</>}
        {reduced && <><br />{t('最近的提醒没有回应，已减少主动服务的次数。')}</>}</>} />
    <SettingRow icon={<FieldTimeOutlined />} label={t('主动服务时间')} description={t('早上和晚上各一次。实际时间可能晚半小时以内。')}
      control={<span className="pa-heartbeat-times">
        <Select aria-label={t('早上时间')} style={{ width: 88 }} value={hours.start} options={timesExcept(hours.end)} onChange={value => void beat({ active_hours: { ...hours, start: value } })} />
        <span aria-hidden="true">–</span>
        <Select aria-label={t('晚上时间')} style={{ width: 88 }} value={hours.end} options={timesExcept(hours.start)} onChange={value => void beat({ active_hours: { ...hours, end: value } })} />
      </span>} />
    <SettingRow icon={<MessageOutlined />} label={t('主动消息推送到')} description={t('主动服务的汇总和定时任务结果会发到这里的私聊。群聊不会收到。')}
      control={<Select aria-label={t('主动消息推送到')} style={{ width: 216 }} value={heartbeat.channel_id || ''}
        options={[{ value: '', label: t('自动（最近私聊的机器人）') }, ...bots.map(bot => ({ value: bot.channel_id, label: bot.display_name || t('我的机器人') })), { value: WEB_ONLY, label: t('只在网页显示') }]}
        onChange={value => void beat({ channel_id: value || null })} />} />
    <SettingRow icon={<EditOutlined />} label={t('主动服务清单')}
      description={<>{t('每行写一件要留意的事。可以改、删、加自己的。')}
        {unavailable.map(key => <span key={key} className="pa-set-note" role="status"><ExclamationCircleOutlined aria-hidden="true" />{t(UNAVAILABLE[key])}</span>)}</>}
      control={<span className="pa-heartbeat-times">
        <Button onClick={() => setEditing(true)}>{t('编辑清单')}</Button>
        <Button icon={<SendOutlined />} loading={running} onClick={() => void runNow()}>{t('立即运行一次')}</Button>
      </span>} />
    <ChecklistEditor open={editing} onClose={() => setEditing(false)} />
  </>;
}

function ChecklistEditor({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [text, setText] = useState('');
  const [etag, setEtag] = useState<string>();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!open) return;
    setLoading(true);
    personalApi.workspaceFile(CHECKLIST)
      .then(file => { setText(file.content || ''); setEtag(file.etag); })
      // A missing file starts empty; saving creates it.
      .catch(() => { setText(''); setEtag(undefined); })
      .finally(() => setLoading(false));
  }, [open]);

  async function submit() {
    setSaving(true);
    try { await personalApi.saveWorkspaceFile(CHECKLIST, text, etag); message.success(t('已保存')); onClose(); }
    catch (exc) { message.error(errorText(exc)); }
    finally { setSaving(false); }
  }
  return <Modal open={open} title={t('主动服务清单')} onCancel={onClose} onOk={() => void submit()} okText={t('保存')} confirmLoading={saving} width={560} destroyOnHidden>
    <p className="pa-hint">{t('例：有等待我批准超过 2 小时的任务时提醒我。需要查邮箱或网页的事，会安排一个后台任务去处理。')}</p>
    <Input.TextArea aria-label={t('主动服务清单')} value={text} disabled={loading} autoSize={{ minRows: 8, maxRows: 18 }} onChange={event => setText(event.target.value)} />
  </Modal>;
}
