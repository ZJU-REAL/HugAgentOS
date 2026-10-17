import { useEffect, useState } from 'react';
import { Button, Input, Modal, Select, Switch, message } from 'antd';
import { EditOutlined, FieldTimeOutlined, HeartOutlined, MessageOutlined, SendOutlined } from '@ant-design/icons';
import { SettingRow } from '../settings/SettingRow';
import { personalApi, errorText, type HeartbeatOutcome, type PersonalChannel, type PersonalHeartbeat as Heartbeat } from '../api';
import { formatShortDateTime } from '../../../utils/date';
import { t } from '../../../i18n';

const CHECKLIST = '/workspace/HEARTBEAT.md';
const INTERVALS = [15, 30, 60, 120, 240];
const TIMES = Array.from({ length: 48 }, (_, index) => {
  const value = String(Math.floor(index / 2)).padStart(2, '0') + ':' + (index % 2 ? '30' : '00');
  return { value, label: value };
});
const OUTCOMES: Record<HeartbeatOutcome, string> = {
  quiet: '没有需要提醒的事',
  alert: '发送了提醒',
  delegated: '安排了一个检查任务',
  duplicate: '与之前的提醒相同，未重复发送',
  failed: '检查失败，下次会再试',
  stale: '等待过久，已跳过',
  skipped: '已跳过',
};
const SKIPS: Record<string, string> = {
  empty_checklist: '清单为空，未运行',
  outside_hours: '不在活跃时段',
  paused: '智能体已暂停',
  busy: '正在回复消息，稍后再查',
  daily_limit: '已达今日上限',
  no_permission: '没有使用权限',
};

function intervalLabel(minutes: number) {
  return minutes < 60 ? t('{count} 分钟', { count: minutes }) : t('{count} 小时', { count: minutes / 60 });
}

function lastLine(heartbeat: Heartbeat) {
  const last = heartbeat.last;
  if (!last) return t('还没有运行过。');
  const what = last.outcome === 'skipped' && last.reason ? t(SKIPS[last.reason] || OUTCOMES.skipped) : t(OUTCOMES[last.outcome] || OUTCOMES.skipped);
  return t('上次 {time}：{what}', { time: formatShortDateTime(last.at), what });
}

interface PersonalHeartbeatProps {
  heartbeat: Heartbeat;
  name: string;
  save: (patch: Record<string, unknown>) => Promise<void>;
  onChange: () => Promise<void>;
}

/** Heartbeat settings: the agent checks HEARTBEAT.md at an interval and is quiet unless something needs the user. */
export function PersonalHeartbeat({ heartbeat, name, save, onChange }: PersonalHeartbeatProps) {
  const [bots, setBots] = useState<PersonalChannel[]>([]);
  const [editing, setEditing] = useState(false);
  const [running, setRunning] = useState(false);
  useEffect(() => { personalApi.channels().then(items => setBots(items.filter(bot => bot.personal_agent))).catch(() => setBots([])); }, []);
  const beat = (patch: Record<string, unknown>) => save({ heartbeat: patch });
  const hours = heartbeat.active_hours;
  const intervals = INTERVALS.filter(value => value >= heartbeat.min_minutes);

  async function runNow() {
    setRunning(true);
    try { await personalApi.runHeartbeat(); message.success(t('{name} 正在检查清单。有需要你关注的事才会发消息。', { name })); await onChange(); }
    catch (exc) { message.error(errorText(exc)); }
    finally { setRunning(false); }
  }

  return <>
    <SettingRow icon={<HeartOutlined />} label={t('心跳')}
      description={<>{t('按间隔检查心跳清单。没事不打扰，有需要你关注的事才发消息。')}<br />{lastLine(heartbeat)}
        {heartbeat.enabled && heartbeat.next_run && <> · {t('下次 {time}', { time: formatShortDateTime(heartbeat.next_run) })}</>}</>}
      control={<Switch aria-label={t('心跳')} checked={heartbeat.enabled} onChange={value => void beat({ enabled: value })} />} />
    <SettingRow icon={<FieldTimeOutlined />} label={t('检查间隔与活跃时段')} description={t('活跃时段外不检查。开始和结束相同表示全天。')}
      control={<span className="pa-heartbeat-times">
        <Select aria-label={t('检查间隔')} style={{ width: 104 }} value={heartbeat.every_minutes}
          options={[...new Set([...intervals, heartbeat.every_minutes])].sort((a, b) => a - b).map(value => ({ value, label: intervalLabel(value) }))}
          onChange={value => void beat({ every_minutes: value })} />
        <Select aria-label={t('开始时间')} style={{ width: 88 }} value={hours.start} options={TIMES} onChange={value => void beat({ active_hours: { ...hours, start: value } })} />
        <span aria-hidden="true">–</span>
        <Select aria-label={t('结束时间')} style={{ width: 88 }} value={hours.end} options={TIMES} onChange={value => void beat({ active_hours: { ...hours, end: value } })} />
      </span>} />
    <SettingRow icon={<MessageOutlined />} label={t('主动消息推送到')} description={t('心跳提醒和定时任务结果也会发到这个机器人的私聊。群聊不会收到。')}
      control={<Select aria-label={t('主动消息推送到')} style={{ width: 180 }} value={heartbeat.channel_id || ''}
        options={[{ value: '', label: t('只在网页显示') }, ...bots.map(bot => ({ value: bot.channel_id, label: bot.display_name || t('我的机器人') }))]}
        onChange={value => void beat({ channel_id: value || null })} />} />
    <SettingRow icon={<EditOutlined />} label={t('心跳清单')} description={t('每行写一个要检查的事项。清单为空时不运行。')}
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
  return <Modal open={open} title={t('心跳清单')} onCancel={onClose} onOk={() => void submit()} okText={t('保存')} confirmLoading={saving} width={560} destroyOnHidden>
    <p className="pa-hint">{t('例：有等待我批准超过 2 小时的任务时提醒我。需要查邮箱或网页的事项，会安排一个后台任务去查。')}</p>
    <Input.TextArea aria-label={t('心跳清单')} value={text} disabled={loading} autoSize={{ minRows: 8, maxRows: 18 }} onChange={event => setText(event.target.value)} />
  </Modal>;
}
