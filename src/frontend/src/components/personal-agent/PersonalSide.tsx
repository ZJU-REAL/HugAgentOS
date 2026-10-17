import { useMemo } from 'react';
import { Button, Tooltip } from 'antd';
import { CloseOutlined, EditOutlined, SettingOutlined } from '@ant-design/icons';
import { PersonalAvatar } from './shared/PersonalAvatar';
import { ChannelEntry } from './channels/ChannelEntry';
import { PersonalComputers } from './computer/PersonalComputers';
import { RecentActivity } from './tasks/RecentActivity';
import { PersonalSchedules } from './scheduling/PersonalSchedules';
import { PersonalOutputs } from './workspace/PersonalOutputs';
import { isTerminal, needsUser } from './tasks/taskState';
import { type PersonalMonitor, type PersonalTask, type PersonalConversation } from './api';
import { t } from '../../i18n';


interface PersonalSideProps {
  monitor?: PersonalMonitor;
  name: string;
  conversations?: PersonalConversation[];
  selectedChatId?: string;
  onSelectChat?: (id?: string) => void;
  open: boolean;
  onClose: () => void;
  onProfile: () => void;
  onSettings: () => void;
  onOpenTask: (task: PersonalTask) => void;
  onRefresh: () => Promise<void>;
  onError: (exc: unknown) => void;
}

/** Status, channels, computers, activity, schedules and outputs, in the order a user scans them. */
export function PersonalSide({ monitor, name, open, onClose, onProfile, onSettings, onOpenTask, onRefresh, onError, conversations = [], selectedChatId, onSelectChat }: PersonalSideProps) {
  const tasks = useMemo(() => {
    const rows = monitor?.tasks || [];
    return [...rows.filter(task => !isTerminal(task)), ...rows.filter(isTerminal)];
  }, [monitor?.tasks]);
  const running = tasks.some(task => task.status === 'running' || task.status === 'queued');
  const attention = tasks.some(task => needsUser(task) && task.status !== 'failed');
  const status = running ? 'thinking' : attention ? 'attention' : 'idle';
  const statusText = { thinking: t('思考中'), attention: t('等待你处理'), idle: t('随时待命') }[status];

  return <aside className={'pa-side' + (open ? ' pa-side--open' : '')} aria-label={t('Personal Agent 状态')}>
    <header className="pa-side-profile">
      <Tooltip title={t('修改头像与名称')}>
        <button type="button" className="pa-side-avatar" onClick={onProfile} aria-label={t('修改头像与名称')}>
          <PersonalAvatar avatar={monitor?.agent?.config.avatar} name={name} size={64} />
          <span className="pa-side-avatar-edit"><EditOutlined /></span>
        </button>
      </Tooltip>
      <div className="pa-side-identity">
        <strong title={name}>{name}</strong>
        <span className={'pa-side-status pa-side-status--' + status} aria-live="polite">{statusText}{status === 'thinking' && <span className="pa-side-ellipsis" aria-hidden="true" />}</span>
      </div>
      <Button type="text" icon={<SettingOutlined />} aria-label={t('设置')} onClick={onSettings} />
      <Button type="text" className="pa-side-close" icon={<CloseOutlined />} aria-label={t('关闭')} onClick={onClose} />
    </header>
    <ChannelEntry agentName={name} conversations={conversations} selectedChatId={selectedChatId} onSelectChat={onSelectChat} />
    <section className="pa-side-section" data-monitor-section="computers"><h3>{t('电脑')}</h3>
      <PersonalComputers agentName={name} devices={monitor?.devices || []} status={monitor?.agent?.computer_status} onChange={onRefresh} />
    </section>
    <section className="pa-side-section" data-monitor-section="tasks"><h3>{t('任务')}</h3><RecentActivity tasks={tasks} onOpen={onOpenTask} onDone={onRefresh} onError={onError} /></section>
    <section className="pa-side-section" data-monitor-section="schedules"><h3>{t('定时任务')}</h3>
      <PersonalSchedules items={monitor?.schedules || []} onDone={onRefresh} onError={onError} /></section>
    <section className="pa-side-section" data-monitor-section="outputs"><h3>{t('输出')}</h3><PersonalOutputs page={monitor?.outputs} /></section>
  </aside>;
}
