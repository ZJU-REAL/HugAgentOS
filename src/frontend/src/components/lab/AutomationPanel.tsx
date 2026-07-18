import { useEffect } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Button, Empty } from 'antd';
import { PlusOutlined, ArrowLeftOutlined } from '@ant-design/icons';
import { useAutomationStore } from '../../stores';
import type { AutomationTask } from '../../types';
import { useDelayedFlag } from '../../hooks';
import { EASE } from '../../utils/motionTokens';
import { AutomationCard } from './AutomationCard';
import { AutomationCreateModal } from './AutomationCreateModal';
import { AutomationDetailPage } from './AutomationDetailPage';
import { AutomationListSkeleton } from './AutomationSkeleton';
import '../../styles/automation.css';
import { t } from '../../i18n';

interface Props {
  onBack: () => void;
}

export function AutomationPanel({ onBack }: Props) {
  const {
    tasks,
    loading,
    createModalOpen,
    selectedTaskId,
    fetchTasks,
    setCreateModalOpen,
    setSelectedTaskId,
  } = useAutomationStore();

  useEffect(() => {
    void fetchTasks();
  }, [fetchTasks]);

  const showListSkeleton = useDelayedFlag(loading && tasks.length === 0);

  // ── Detail view ──
  if (selectedTaskId) {
    return (
      <AutomationDetailPage
        taskId={selectedTaskId}
        onBack={() => {
          setSelectedTaskId(null);
          void fetchTasks();
        }}
      />
    );
  }

  // ── List view ──
  const activeTasks = tasks.filter((t) => t.status === 'active');
  const pausedTasks = tasks.filter((t) => t.status === 'paused');
  const otherTasks = tasks.filter((t) => !['active', 'paused'].includes(t.status));

  // Task-card add/remove animation: popLayout + layout, key=task_id;
  // when polling/fetchTasks replaces the whole group, the stable key prevents remounts; on deletion the remaining cards smoothly fill in.
  // initial=false: the first-screen list does not play an entrance animation, only subsequent add/remove animations.
  const renderTaskCards = (list: AutomationTask[]) => (
    <AnimatePresence mode="popLayout" initial={false}>
      {list.map((task) => (
        <motion.div
          key={task.task_id}
          layout
          initial={{ opacity: 0, scale: 0.98 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.96, transition: { duration: 0.2, ease: EASE.standard } }}
          transition={{ duration: 0.2, ease: EASE.standard }}
        >
          <AutomationCard task={task} onClick={() => setSelectedTaskId(task.task_id)} />
        </motion.div>
      ))}
    </AnimatePresence>
  );

  return (
    <div className="jx-agentPage">
      <div className="jx-agentPage-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <Button
            type="text"
            icon={<ArrowLeftOutlined />}
            onClick={onBack}
            style={{ marginRight: 4 }}
          />
          <div>
            <div className="jx-agentPage-title">{t('自动化')}</div>
            <div className="jx-agentPage-subtitle">{t('设置定时或周期性 AI 任务，到时间后自动执行')}</div>
          </div>
        </div>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => setCreateModalOpen(true)}
        >
          {t('创建自动化')}
        </Button>
      </div>

      <div className="jx-automation-body">
        {showListSkeleton ? (
          <AutomationListSkeleton />
        ) : loading && tasks.length === 0 ? null : tasks.length === 0 ? (
          <Empty
            description={t('暂无自动化任务')}
            style={{ marginTop: 80 }}
          >
            <Button type="primary" onClick={() => setCreateModalOpen(true)}>
              {t('创建第一个自动化任务')}
            </Button>
          </Empty>
        ) : (
          <>
            {activeTasks.length > 0 && (
              <div className="jx-automation-section">
                <div className="jx-automation-sectionTitle">{t('运行中')}</div>
                {renderTaskCards(activeTasks)}
              </div>
            )}
            {pausedTasks.length > 0 && (
              <div className="jx-automation-section">
                <div className="jx-automation-sectionTitle">{t('已暂停')}</div>
                {renderTaskCards(pausedTasks)}
              </div>
            )}
            {otherTasks.length > 0 && (
              <div className="jx-automation-section">
                <div className="jx-automation-sectionTitle">{t('已完成 / 已停用')}</div>
                {renderTaskCards(otherTasks)}
              </div>
            )}
          </>
        )}
      </div>

      <AutomationCreateModal
        open={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        onCreated={() => {
          setCreateModalOpen(false);
          void fetchTasks();
        }}
      />
    </div>
  );
}
