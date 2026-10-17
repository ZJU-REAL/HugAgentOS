import { useState } from 'react';
import { Button, Popconfirm, Tooltip } from 'antd';
import { ClockCircleOutlined, DeleteOutlined, SettingOutlined, SyncOutlined } from '@ant-design/icons';
import { personalApi, type PersonalSchedule } from '../api';
import { PersonalScheduleEditor } from './PersonalScheduleEditor';
import { t } from '../../../i18n';
import { formatShortDateTime } from '../../../utils/date';

/** Owner-managed schedules, always visible below the task board. */
export function PersonalSchedules({ items, onDone, onError }: { items: PersonalSchedule[]; onDone: () => Promise<void>; onError: (exc: unknown) => void }) {
  const [editing, setEditing] = useState<PersonalSchedule>();
  const [deleting, setDeleting] = useState<string>();
  async function remove(id: string) {
    if (deleting) return;
    setDeleting(id);
    try { await personalApi.deleteSchedule(id); await onDone(); } catch (exc) { onError(exc); } finally { setDeleting(undefined); }
  }
  return <>
    {!items.length && <p className="pa-side-empty">{t('暂无定时任务')}</p>}
    <ul className="pa-activity">{items.map(item => <li key={item.id} className="pa-schedule" data-testid="pa-schedule">
      <Tooltip title={(item.cron ? t('重复') + ' · ' + item.cron : t('一次')) + ' · ' + item.timezone + ' — ' + item.instruction} placement="left" mouseEnterDelay={0.4}>
        <button type="button" className="pa-activity-row" onClick={() => setEditing(item)}>
          {item.cron ? <SyncOutlined className="pa-activity-icon" aria-label={t('重复')} /> : <ClockCircleOutlined className="pa-activity-icon" aria-label={t('一次')} />}
          <span className="pa-activity-title">{item.title}</span>
          <small>{item.enabled === false ? t('已暂停') : formatShortDateTime(item.next_run, t('已结束'))}</small>
        </button>
      </Tooltip>
      <Button type="text" size="small" icon={<SettingOutlined />} aria-label={t('定时任务设置')} onClick={() => setEditing(item)} />
      <Popconfirm title={t('删除此定时任务？')} onConfirm={() => remove(item.id)} okText={t('删除')} cancelText={t('取消')}>
        <Button type="text" size="small" icon={<DeleteOutlined />} loading={deleting === item.id} disabled={!!deleting} aria-label={t('删除定时任务')} />
      </Popconfirm>
    </li>)}</ul>
    {editing && <PersonalScheduleEditor key={editing.id} item={editing} onClose={() => setEditing(undefined)} onSaved={onDone} />}
  </>;
}
