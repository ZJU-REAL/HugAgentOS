import { useCallback, useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Button, Drawer, Empty, Modal, Popconfirm, Progress, Switch, Tag, message } from 'antd';
import { ArrowLeftOutlined, PlusOutlined } from '@ant-design/icons';
import { DUR, EASE } from '../../utils/motionTokens';
import { useStatusFlash } from '../../hooks/useFlash';
import { useSkillDistillStore } from '../../stores';
import { saveSkillDistillJob, type SkillDistillJob } from '../../api';
import { SkillDistillCreateModal } from './SkillDistillCreateModal';
import '../../styles/skill-distill.css';
import { t } from '../../i18n';

interface SkillDistillPanelProps {
  onBack: () => void;
}

const STATUS_META: Record<string, { label: string; color: string }> = {
  queued: { label: t('排队中'), color: 'default' },
  running: { label: t('蒸馏中'), color: 'processing' },
  completed: { label: t('已完成'), color: 'success' },
  failed: { label: t('失败'), color: 'error' },
  cancelled: { label: t('已取消'), color: 'default' },
};

const POLL_INTERVAL_MS = 3000;

export function SkillDistillPanel({ onBack }: SkillDistillPanelProps) {
  const {
    jobs, loading, fetchJobs, refreshJobs,
    detailJob, detailLoading, openDetail, closeDetail,
    cancelJob, removeJob, applySavedJob,
  } = useSkillDistillStore();
  const [createOpen, setCreateOpen] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [enableOnSave, setEnableOnSave] = useState(true);
  const [saving, setSaving] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // The state-machine animation binds only to the status diff, not to render: when the 3s poll replaces the whole array,
  // compare against the previous round's status snapshot to catch the exact moment of the running→completed flip.
  const justCompletedIds = useStatusFlash(
    jobs,
    (j) => j.job_id,
    (j) => j.status,
    (_prev, next) => next === 'completed',
    1600,
  );

  // 3s polling for in-progress jobs
  const ensurePolling = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = setInterval(async () => {
      const active = await refreshJobs();
      if (!active && pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    }, POLL_INTERVAL_MS);
  }, [refreshJobs]);

  useEffect(() => {
    fetchJobs();
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [fetchJobs]);

  useEffect(() => {
    if (jobs.some((j) => j.status === 'queued' || j.status === 'running')) {
      ensurePolling();
    }
  }, [jobs, ensurePolling]);

  useEffect(() => {
    if (detailJob) {
      setEditContent(detailJob.result_skill_content || '');
      setEnableOnSave(true);
    }
  }, [detailJob]);

  const handleCancel = async (jobId: string) => {
    try {
      await cancelJob(jobId);
      message.success(t('已取消'));
    } catch (e) {
      message.error(t('取消失败：{msg}', { msg: (e as Error).message }));
    }
  };

  const handleDelete = async (jobId: string) => {
    try {
      await removeJob(jobId);
      message.success(t('已删除'));
    } catch (e) {
      message.error(t('删除失败：{msg}', { msg: (e as Error).message }));
    }
  };

  const handleSave = async () => {
    if (!detailJob) return;
    setSaving(true);
    try {
      const res = await saveSkillDistillJob(detailJob.job_id, {
        skill_content: editContent.trim() || undefined,
        enable: enableOnSave,
      });
      applySavedJob(res.job);
      Modal.success({
        title: t('已保存为我的技能'),
        content: `${res.display_name} (${res.skill_id}) ${res.is_enabled ? t('已启用') : t('已保存')}`,
      });
      closeDetail();
    } catch (e) {
      message.error(t('保存失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setSaving(false);
    }
  };

  const renderJobCard = (job: SkillDistillJob) => {
    const meta = STATUS_META[job.status] || STATUS_META.queued;
    const active = job.status === 'queued' || job.status === 'running';
    const justCompleted = justCompletedIds.has(job.job_id);
    const title =
      job.result_meta?.display_name ||
      (job.scope?.chat_ids ? `${job.scope.chat_ids.length} 个会话` : '全部会话');
    const percent = job.progress_total
      ? Math.round((job.progress_done / job.progress_total) * 100)
      : 0;

    return (
      <div
        className={
          'jx-skillDistill-job'
          + (job.status === 'running' ? ' jx-skillDistill-job--running' : '')
          + (justCompleted ? ' jx-anim-flash' : '')
        }
      >
        <div className="jx-skillDistill-job-head">
          <span className="jx-skillDistill-job-title">{title}</span>
          {/* key=status: remount when status flips, play the statusIn settle animation once */}
          <Tag key={job.status} className="jx-anim-statusIn" color={meta.color}>{meta.label}</Tag>
          {job.result_meta?.partial ? <Tag color="warning">{t('部分语料')}</Tag> : null}
          {job.saved_skill_id ? <Tag color="blue">{t('已保存：{id}', { id: job.saved_skill_id })}</Tag> : null}
        </div>
        {active ? (
          <Progress
            percent={percent}
            size="small"
            format={() => `${job.progress_done}/${job.progress_total || '?'}`}
          />
        ) : null}
        {job.status === 'failed' && job.error ? (
          <div className="jx-skillDistill-job-error">{job.error}</div>
        ) : null}
        {job.status === 'completed' && job.result_meta?.description ? (
          <div className="jx-skillDistill-job-desc">{job.result_meta.description}</div>
        ) : null}
        <div className="jx-skillDistill-job-foot">
          <span className="jx-skillDistill-job-time">
            {job.created_at ? new Date(job.created_at).toLocaleString('zh-CN') : ''}
          </span>
          <span className="jx-skillDistill-job-actions">
            {active ? (
              <Button size="small" onClick={() => handleCancel(job.job_id)}>{t('取消')}</Button>
            ) : null}
            {job.status === 'completed' ? (
              <Button
                size="small"
                type="primary"
                className={justCompleted ? 'jx-anim-statusIn' : undefined}
                onClick={() => openDetail(job.job_id)}
              >
                {job.saved_skill_id ? t('查看产物') : t('预览并保存')}
              </Button>
            ) : null}
            {!active ? (
              <Popconfirm title={t('删除该作业记录？')} onConfirm={() => handleDelete(job.job_id)}>
                <Button size="small" danger>{t('删除')}</Button>
              </Popconfirm>
            ) : null}
          </span>
        </div>
      </div>
    );
  };

  return (
    <div className="jx-agentPage">
      <div className="jx-agentPage-header">
        <div>
          <div className="jx-agentPage-title">
            <Button type="text" icon={<ArrowLeftOutlined />} onClick={onBack} />
            {t('个人技能蒸馏')}
          </div>
          <div className="jx-agentPage-subtitle">
            {t('从你的历史会话中提炼可复用的个人技能，保存后仅自己可见可用')}
          </div>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          {t('新建蒸馏')}
        </Button>
      </div>

      <div className="jx-skillDistill-list">
        {jobs.length === 0 && !loading ? (
          <Empty className="jx-anim-fadeIn" description={t('还没有蒸馏作业，点击右上角「新建蒸馏」开始')} />
        ) : (
          /* initial={false}: the first-screen full list and poll replacements don't play an entrance; only newly inserted items at the list head
             play height 0→auto; key=job_id, driven by the business id, never bound to render. */
          <AnimatePresence initial={false}>
            {jobs.map((job) => (
              <motion.div
                key={job.job_id}
                layout="position"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0, transition: { duration: DUR.fast, ease: EASE.exit } }}
                transition={{ duration: DUR.normal, ease: EASE.brandOut }}
                style={{ overflow: 'hidden', flexShrink: 0 }}
              >
                {renderJobCard(job)}
              </motion.div>
            ))}
          </AnimatePresence>
        )}
      </div>

      <SkillDistillCreateModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={() => fetchJobs()}
      />

      <Drawer
        title={detailJob?.result_meta?.display_name || t('蒸馏产物')}
        open={!!detailJob || detailLoading}
        onClose={closeDetail}
        width={680}
        footer={
          detailJob && !detailJob.saved_skill_id ? (
            <div className="jx-skillDistill-drawer-footer">
              <span>
                {t('保存后立即启用')}&nbsp;
                <Switch size="small" checked={enableOnSave} onChange={setEnableOnSave} />
              </span>
              <Button type="primary" loading={saving} onClick={handleSave}>
                {t('保存为我的技能')}
              </Button>
            </div>
          ) : null
        }
      >
        {detailJob ? (
          <div className="jx-skillDistill-detail">
            {detailJob.result_meta?.digest_text ? (
              <div className="jx-skillDistill-detail-digest">{detailJob.result_meta.digest_text}</div>
            ) : null}
            <div className="jx-skillDistill-detail-hintRow">
              {t('SKILL.md 内容（可在保存前直接编辑）')}
              {typeof detailJob.result_meta?.confidence === 'number' ? (
                <Tag>{t('可复用度 {pct}%', { pct: (detailJob.result_meta.confidence * 100).toFixed(0) })}</Tag>
              ) : null}
            </div>
            <textarea
              className="jx-skillDistill-detail-editor"
              value={editContent}
              onChange={(e) => setEditContent(e.target.value)}
              readOnly={!!detailJob.saved_skill_id}
              spellCheck={false}
            />
          </div>
        ) : null}
      </Drawer>
    </div>
  );
}
