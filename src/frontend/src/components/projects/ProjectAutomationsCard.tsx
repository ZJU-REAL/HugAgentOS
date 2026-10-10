import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Spin, message } from 'antd';
import { ClockCircleOutlined, ReloadOutlined, SettingOutlined } from '@ant-design/icons';
import { automationAvailabilityWarning, getAutomationRuns, listAutomations } from '../../api';
import { useAuthStore } from '../../stores/authStore';
import { prepareRunChat } from '../../stores/automationChatStore';
import { navigateTo, pathForAutomationChat, pathForPanel } from '../../routing/navigation';
import type { AutomationRun, AutomationTask, ProjectDetail } from '../../types';
import { formatShortDateTime } from '../../utils/date';
import { t } from '../../i18n';

type TaskEntry = { task: AutomationTask; latest?: AutomationRun; error?: string };

export default function ProjectAutomationsCard({ project }: { project: ProjectDetail }) {
  const userId = useAuthStore(s => s.authUser?.user_id);
  const generation = useRef(0);
  const opening = useRef(false);
  const contextGeneration = useRef(0);
  const [entries, setEntries] = useState<TaskEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [openingId, setOpeningId] = useState('');
  const reload = useCallback(async () => {
    const version = ++generation.current;
    setLoading(true);
    setError('');
    try {
      const tasks = (await listAutomations()).filter(task =>
        task.project_id === project.project_id &&
        (task.execution_location === 'local') === ((project.kind as string) === 'local'));
      const results: TaskEntry[] = new Array(tasks.length);
      let next = 0;
      // Limit outstanding run requests even when a project has many schedules.
      await Promise.all(Array.from({ length: Math.min(4, tasks.length) }, async () => {
        while (next < tasks.length && version === generation.current) {
          const index = next++;
          const task = tasks[index];
          try {
            const runs = await getAutomationRuns(task.task_id, 1);
            results[index] = { task, latest: runs[0] };
          } catch (failure) {
            results[index] = { task, error: failure instanceof Error ? failure.message : t('加载失败') };
          }
        }
      }));
      if (version !== generation.current) return;
      setEntries(results);
      setError(automationAvailabilityWarning());
    } catch (failure) {
      if (version === generation.current) setError(failure instanceof Error ? failure.message : t('加载失败'));
    } finally {
      if (version === generation.current) setLoading(false);
    }
  }, [project.project_id, project.kind]);

  useEffect(() => {
    void reload();
    const onFocus = () => { if (!document.hidden) void reload(); };
    window.addEventListener('focus', onFocus);
    return () => { contextGeneration.current += 1; generation.current += 1; window.removeEventListener('focus', onFocus); };
  }, [reload, userId]);

  const openLatest = async (task: AutomationTask) => {
    if (opening.current) return;
    const version = contextGeneration.current;
    opening.current = true;
    setOpeningId(task.task_id);
    try {
      const [latest] = await getAutomationRuns(task.task_id, 1);
      if (version !== contextGeneration.current) return;
      if (!latest?.chat_id) {
        message.info(t('暂无执行记录'));
        return;
      }
      const id = latest.chat_id;
      prepareRunChat(task.task_id, task.name || t('定时任务'), latest, {
        projectId: project.project_id, projectName: project.name,
      });
      navigateTo(pathForAutomationChat(task.task_id, id));
    } catch (failure) {
      if (version === contextGeneration.current) message.error(failure instanceof Error ? failure.message : t('加载失败'));
    } finally {
      opening.current = false;
      if (version === contextGeneration.current) setOpeningId('');
    }
  };

  return <section className="jx-projectRail-card" aria-label={t('项目定时任务')}>
    <div className="jx-projectRail-cardHeader">
      <div className="jx-projectRail-cardTitle">{t('项目定时任务')}</div>
      <Button size="small" type="text" icon={<ReloadOutlined />} loading={loading}
        aria-label={t('刷新项目定时任务')} onClick={() => void reload()} />
    </div>
    {error && <div role="alert">{error}<Button size="small" onClick={() => void reload()}>{t('重试')}</Button></div>}
    {loading && entries.length === 0 ? <Spin size="small" /> : entries.length === 0 && !error
      ? <div className="jx-projectRail-cardEmpty">{t('本项目暂无定时任务')}</div>
      : <div className="jx-projectAutomation-list">
        {entries.map(({ task, latest, error: runError }) => <div className="jx-projectAutomation-row" key={task.task_id}>
          <button type="button" className="jx-projectAutomation-chat" disabled={openingId === task.task_id}
            onClick={() => void openLatest(task)}>
            <ClockCircleOutlined />
            <span className="jx-projectAutomation-body">
              <span className="jx-projectAutomation-name">{task.name || t('未命名任务')}</span>
              <span className="jx-projectRail-cardSub">{runError || (latest
                ? formatShortDateTime(latest.started_at, '—') : t('暂无执行记录'))}</span>
              {latest && <span className="jx-projectRail-cardSub">{latest.status === 'running' ? t('运行中') : latest.status === 'failed' ? t('执行失败') : t('执行成功')}</span>}
              {latest?.result_summary && <span className="jx-projectAutomation-summary">{latest.result_summary}</span>}
            </span>
          </button>
          <Button type="text" size="small" icon={<SettingOutlined />} aria-label={t('任务详情')}
            onClick={() => navigateTo(pathForPanel('automation', task.task_id))} />
        </div>)}
      </div>}
  </section>;
}
