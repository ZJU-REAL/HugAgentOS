import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Empty, InputNumber, Modal, Tag, message } from 'antd';
import { ReloadOutlined, RollbackOutlined } from '@ant-design/icons';
import { personalApi, errorText, type MemorySnapshot, type PersonalDreamingStatus } from '../api';
import { subscribePersonalChanges } from '../shared/liveFeed';
import { LoadError, SkeletonLines } from '../../common/LoadState';
import { userMarkdownHtml } from '../../../utils/userMarkdown';
import { t } from '../../../i18n';
import { DREAMING_STATES, formatTime } from './memoryLabels';
import { latestReport } from './memoryText';

/** Fields this view shows itself; anything else the backend adds is listed under "更多状态". */
const KNOWN = new Set(['hour', 'timezone', 'next_run', 'run_id', 'status', 'started_at', 'attempts',
  'next_retry', 'error', 'outcomes', 'completed_at']);

function extraValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value) && value.every((v) => typeof v === 'string' || typeof v === 'number')) return value.join('、');
  try { return JSON.stringify(value); } catch { return String(value); }
}

interface PersonalDreamingProps {
  onChanged?: () => void;
}

/** Dreaming: status and schedule, run once now, the latest report and the snapshots to restore. */
export function PersonalDreaming({ onChanged }: PersonalDreamingProps) {
  const [status, setStatus] = useState<PersonalDreamingStatus | null | undefined>(undefined);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<string | null | undefined>(undefined);
  const [showAll, setShowAll] = useState(false);
  const [snapshots, setSnapshots] = useState<MemorySnapshot[] | null>(null);
  const [snapshotError, setSnapshotError] = useState('');
  const [confirming, setConfirming] = useState<string | null>(null);
  const [restoring, setRestoring] = useState(false);
  const [restoreError, setRestoreError] = useState('');

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try { const next = await personalApi.dreaming(signal); if (!signal?.aborted) { setStatus(next); setError(''); } }
    catch (exc) { if (!signal?.aborted) setError(errorText(exc)); }
  }, []);
  const loadReport = useCallback(async (signal?: AbortSignal) => {
    try { const file = await personalApi.workspaceFile('DREAMS.md', signal); if (!signal?.aborted) setReport(file.content ?? ''); }
    catch { if (!signal?.aborted) setReport(null); }
  }, []);
  const loadSnapshots = useCallback(async (signal?: AbortSignal) => {
    try { const data = await personalApi.memorySnapshots(signal); if (!signal?.aborted) { setSnapshots(data.items ?? []); setSnapshotError(''); } }
    catch (exc) { if (!signal?.aborted) setSnapshotError(errorText(exc)); }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal); void loadReport(controller.signal); void loadSnapshots(controller.signal);
    // A run's progress is written to the agent's own conversation and settings, and the server signals each write.
    const unsubscribe = subscribePersonalChanges(signal => { if (signal.type !== 'down') void refresh(controller.signal); });
    return () => { controller.abort(); unsubscribe(); };
  }, [refresh, loadReport, loadSnapshots]);

  // When a run this view watched finishes, its report and snapshot are new and pages changed.
  const runState = status?.status;
  const previous = useRef(runState);
  useEffect(() => {
    const was = previous.current;
    previous.current = runState;
    if ((was === 'running' || was === 'pending') && runState !== was) { void loadReport(); void loadSnapshots(); onChanged?.(); }
  }, [runState, loadReport, loadSnapshots, onChanged]);

  const update = async (patch: { hour?: number }) => {
    setBusy(true); setError('');
    try { await personalApi.settings({ dreaming: patch }); await refresh(); }
    catch (exc) { setError(errorText(exc)); }
    finally { setBusy(false); }
  };
  const run = async () => {
    setBusy(true); setError('');
    try { setStatus(await personalApi.runDreaming()); }
    catch (exc) { setError(errorText(exc)); }
    finally { setBusy(false); }
  };
  const confirmRun = () => Modal.confirm({
    title: t('现在整理一次记忆？'),
    content: t('会读还没整理的对话和任务，核对并整理记忆页面，通常要几分钟到十几分钟。整理期间智能体暂时不能写记忆页面；整理前会先存一份快照，可以恢复。'),
    okText: t('开始整理'), cancelText: t('取消'),
    onOk: () => run(),
  });
  const restore = async (id: string) => {
    setRestoring(true); setRestoreError('');
    try {
      const result = await personalApi.restoreMemorySnapshot(id);
      message.success(result.changed.length ? t('已恢复，{n} 个文件变回了快照里的样子', { n: result.changed.length }) : t('已恢复，记忆和快照本来就一样'));
      setConfirming(null);
      onChanged?.();
    } catch (exc) { setRestoreError(errorText(exc)); }
    finally { setRestoring(false); }
  };

  const zone = status?.timezone;
  const extras = useMemo(() => Object.entries(status ?? {}).filter(([key, value]) => !KNOWN.has(key) && value !== null && value !== undefined), [status]);
  const shown = useMemo(() => (report ? latestReport(report) : null), [report]);
  const active = runState === 'running' || runState === 'pending';

  return <div className="pa-mem-dreaming">
    <section className="pa-mem-card" aria-label={t('Dreaming 状态')}>
      {status === undefined && !error ? <SkeletonLines rows={3} /> : status === null ? <p className="pa-mem-hint">{t('个人智能体还没有创建。')}</p> : status && <>
        <div className="pa-mem-cardHead">
          <div>
            <h3>{t('Dreaming · 每晚整理记忆')}</h3>
            <p className="pa-mem-hint">{t('读当天的对话补上漏记的事，核对白天写下的内容，再把页面整理成目录。整理前会先存一份快照。')}</p>
          </div>
          <Button type="primary" loading={busy && !active} disabled={active} onClick={confirmRun}>
            {t(runState === 'failed' || runState === 'retry' ? '重试整理' : '整理一次')}
          </Button>
        </div>
        <dl className="pa-mem-stats">
          <dt>{t('状态')}</dt>
          <dd>{runState ? <Tag variant="filled" color={runState === 'failed' ? 'error' : active ? 'processing' : 'default'}>{t(DREAMING_STATES[runState] || runState)}</Tag> : '—'}</dd>
          <dt>{t('上次完成')}</dt><dd>{formatTime(status.completed_at, zone)}</dd>
          <dt>{t('下次整理')}</dt><dd>{formatTime(status.next_run, zone)}</dd>
          <dt>{t('每天整理时间')}</dt>
          <dd className="pa-mem-schedule">
            <InputNumber size="small" aria-label={t('每天整理时间')} min={0} max={23} precision={0} value={status.hour}
              disabled={busy} onChange={(hour) => { if (hour !== null) void update({ hour }); }} />
            <span className="pa-mem-hint">:00 · {status.timezone}</span>
          </dd>
        </dl>
        {status.outcomes && status.outcomes.length > 0 && <ul className="pa-mem-outcomes">{status.outcomes.map((o, i) => <li key={i}>{o}</li>)}</ul>}
        {(runState === 'retry' || runState === 'failed') && <Alert type="warning" showIcon title={t('整理未完成，已保留进度；可重试。')}
          description={<>{status.error && <div>{status.error}</div>}{runState === 'retry' ? t('下次重试：{time}', { time: formatTime(status.next_retry, zone) }) : t('已达到自动重试上限，请检查模型或工作区后重试。')}</>} />}
        {extras.length > 0 && <details className="pa-mem-extras"><summary>{t('更多状态')}</summary>
          <dl className="pa-mem-stats">{extras.map(([key, value]) => <Fragment key={key}><dt><code>{key}</code></dt><dd>{extraValue(value)}</dd></Fragment>)}</dl>
        </details>}
      </>}
      {error && <Alert type="error" showIcon title={error} />}
    </section>

    <section className="pa-mem-card" aria-label={t('最近一次整理报告')}>
      <div className="pa-mem-cardHead">
        <h3>{t('最近一次整理报告')}</h3>
        <span className="pa-mem-confirmActions">
          {shown?.more && <Button size="small" type="text" onClick={() => setShowAll(!showAll)}>{showAll ? t('只看最近一次') : t('查看全部报告')}</Button>}
          <Button size="small" type="text" icon={<ReloadOutlined />} aria-label={t('刷新')} onClick={() => void loadReport()} />
        </span>
      </div>
      {report === undefined ? <SkeletonLines rows={4} />
        : !report || !report.trim() ? <p className="pa-mem-hint">{t('还没有整理报告。第一次 Dreaming 完成后，这里会说明它补了什么、改了什么、哪些需要你看一眼。')}</p>
        : <div className="jx-md pa-mem-report" dangerouslySetInnerHTML={{ __html: userMarkdownHtml((showAll ? report : shown?.report ?? report).replace(/\[\[([a-z0-9-]+)\]\]/g, '`$1`')) }} />}
    </section>

    <section className="pa-mem-card" aria-label={t('快照')}>
      <div className="pa-mem-cardHead">
        <div>
          <h3>{t('快照')}</h3>
          <p className="pa-mem-hint">{t('每次整理前保存一份记忆页面，保留最近 14 份。恢复会把所有页面变回当时的样子，每个改动都记在修改记录里。')}</p>
        </div>
        <Button size="small" type="text" icon={<ReloadOutlined />} aria-label={t('刷新')} onClick={() => void loadSnapshots()} />
      </div>
      {snapshotError ? <LoadError title={t('快照列表加载失败')} reason={snapshotError} onRetry={() => void loadSnapshots()} />
        : !snapshots ? <SkeletonLines rows={3} />
        : snapshots.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('还没有快照，第一次整理时会创建。')} />
        : <ul className="pa-mem-snapshots">{snapshots.map((snap) => <li key={snap.id} className="pa-mem-snapshot">
          <div className="pa-mem-snapshotRow">
            <span className="pa-mem-snapshotTime">{formatTime(snap.at, zone)}</span>
            <span className="pa-mem-hint">{t('{n} 个文件', { n: snap.files })}{snap.note ? ' · ' + snap.note : ''}</span>
            {confirming !== snap.id && <Button size="small" type="text" icon={<RollbackOutlined />} disabled={active}
              onClick={() => { setConfirming(snap.id); setRestoreError(''); }}>{t('恢复')}</Button>}
          </div>
          {confirming === snap.id && <div className="pa-mem-confirm" role="group" aria-label={t('确认恢复快照')}>
            <span>{t('记忆页面会全部变回这份快照；之后新建的页面会被删除。每个文件的改动都能在修改记录里单独撤销。')}</span>
            <span className="pa-mem-confirmActions">
              <Button size="small" type="text" onClick={() => setConfirming(null)}>{t('取消')}</Button>
              <Button size="small" type="primary" danger loading={restoring} onClick={() => void restore(snap.id)}>{t('恢复到这份快照')}</Button>
            </span>
            {restoreError && <Alert className="pa-mem-confirmError" type="error" showIcon title={restoreError} />}
          </div>}
        </li>)}</ul>}
    </section>
  </div>;
}
