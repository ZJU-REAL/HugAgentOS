import { useEffect, useState } from 'react';
import { Alert, Button, Tag } from 'antd';
import { BookOutlined, HistoryOutlined, MoonOutlined } from '@ant-design/icons';
import { SettingGroup, SettingRow } from '../settings/SettingRow';
import { personalApi, type MemoryPages, type PersonalDreamingStatus } from '../api';
import { t } from '../../../i18n';
import { MemoryCenter, type MemoryTab } from './MemoryCenter';
import { DREAMING_STATES, formatTime } from './memoryLabels';

interface PersonalMemoryProps {
  /** The platform journal's write status from the monitor. */
  status?: { available: boolean; pending: number; retrying: number };
}

/** Settings group for the Personal Agent's memory wiki; the full view opens in the memory center. */
export function PersonalMemory({ status }: PersonalMemoryProps) {
  const [tab, setTab] = useState<MemoryTab | null>(null);
  const [pages, setPages] = useState<MemoryPages | null>(null);
  const [dreaming, setDreaming] = useState<PersonalDreamingStatus | null>(null);

  // A summary only; failures leave the rows without counts, the memory center shows the errors.
  useEffect(() => {
    if (tab !== null) return;
    const controller = new AbortController();
    personalApi.memoryPages(controller.signal).then(setPages).catch(() => undefined);
    personalApi.dreaming(controller.signal).then(setDreaming).catch(() => undefined);
    return () => controller.abort();
  }, [tab]);

  const count = pages?.pages?.length ?? 0;
  const pending = pages?.pages?.filter((p) => p.pending).length ?? 0;
  const runState = dreaming?.status;

  return <SettingGroup title={t('记忆')}>
    {status && (!status.available || status.retrying > 0) && <Alert type="warning" showIcon
      title={t('部分记忆等待写入，工作区恢复可用后会自动重试。')} />}
    <SettingRow icon={<BookOutlined />} label={t('记忆页面')}
      description={pages?.available === false ? t('当前部署未配置个人智能体工作区。')
        : pages ? (count ? t('共 {n} 页', { n: count }) + (pending ? ' · ' + t('{n} 页待整理', { n: pending }) : '') + ' · ' + t('按人、组织、项目、主题与决定、做法分页记录，用到时才读。')
          : t('还没有记忆页面。智能体干活时会把值得记住的事写成页面。'))
        : t('按人、组织、项目、主题与决定、做法分页记录，用到时才读。')}
      control={<Button size="small" onClick={() => setTab('wiki')}>{t('打开记忆')}</Button>} />
    <SettingRow icon={<HistoryOutlined />} label={t('修改记录')}
      description={t('谁在什么时候改了哪一页、依据哪条对话；可以查看对比并撤销，保留 30 天。')}
      control={<Button size="small" onClick={() => setTab('changes')}>{t('查看')}</Button>} />
    <SettingRow icon={<MoonOutlined />} label={t('Dreaming · 每晚整理记忆')}
      description={dreaming ? t('下次整理：{time}', { time: formatTime(dreaming.next_run, dreaming.timezone) })
        : t('读当天的对话补漏、核对白天写下的内容，再整理目录。')}
      action={runState ? <Tag variant="filled" color={runState === 'failed' ? 'error' : 'default'}>{t(DREAMING_STATES[runState] || runState)}</Tag> : undefined}
      control={<Button size="small" onClick={() => setTab('dreaming')}>{t('查看')}</Button>} />
    <MemoryCenter open={tab !== null} tab={tab ?? 'wiki'} onTab={setTab} onClose={() => setTab(null)} />
  </SettingGroup>;
}
