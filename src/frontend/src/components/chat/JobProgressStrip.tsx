import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { CloseOutlined, LoadingOutlined, StopOutlined, WarningOutlined } from '@ant-design/icons';
import { DUR, EASE } from '../../utils/motionTokens';
import { listChatJobs, cancelJobApi, getJobApi } from '../../api';
import type { JobBrief } from '../../types';
import { t } from '../../i18n';

/* ───────────────────────────────────────────
   Job bar — 工作流模式下钉在输入框上方的后台作业状态条。

   为什么必须有：作业跑在后台，主对话是安静的。没有这条，用户根本判断不出
   "工作流模式到底有没有在跑"——只能干等或反复追问，而每次追问都是一轮真实推理。
   这条走 REST 轮询（一次 SQL 聚合），和模型完全无关，所以看进度是零推理成本的。

   与 PlanProgressStrip 的分工：那条是模型自己报的计划步骤，这条是后台作业的
   真实台账。两条可以同时出现，互不干扰。
   ─────────────────────────────────────────── */

/** 作业活着时轮询要跟得上肉眼；不活跃就彻底停表，别给后端凭空加常驻负载。 */
const POLL_MS = 5000;

/** 重新挂载时往回捞多久的「没善终」作业。太长会把几天前的旧账翻出来，太短又盖不住
 *  「跑着跑着去开了个会」这种再正常不过的离开时长。 */
const ENDED_LOOKBACK_MS = 6 * 60 * 60 * 1000;

function ProgressRing({ settled, total }: { settled: number; total: number }) {
  const R = 7;
  const C = 2 * Math.PI * R;
  const frac = total > 0 ? Math.min(1, settled / total) : 0;
  return (
    <svg className="jx-jobStrip-ring" width="18" height="18" viewBox="0 0 18 18" aria-hidden>
      <circle cx="9" cy="9" r={R} fill="none" stroke="var(--color-primary-bg)" strokeWidth="2.5" />
      <circle
        cx="9" cy="9" r={R} fill="none"
        stroke="var(--color-primary)" strokeWidth="2.5" strokeLinecap="round"
        strokeDasharray={C}
        strokeDashoffset={C * (1 - frac)}
        transform="rotate(-90 9 9)"
        style={{ transition: 'stroke-dashoffset .35s ease' }}
      />
    </svg>
  );
}

/** 后端给的 ISO 串按 UTC 解释。
 *
 *  不能直接 `new Date(s)`：JS 规范里**不带时区偏移**的日期时间串按**本地时区**解析，
 *  同一个后端时刻在 UTC+8 的浏览器上会平移 8 小时。后端现在都发带偏移的串，这里做的是
 *  兜底——历史数据或别的入口漏了偏移时，按 UTC 补齐比按本地时区猜要稳。 */
function parseServerTime(raw?: string | null): number {
  if (!raw) return NaN;
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(raw.trim());
  return new Date(hasZone ? raw : `${raw.replace(' ', 'T')}Z`).getTime();
}

function elapsedLabel(startedAt?: string | null): string {
  const ts = parseServerTime(startedAt);
  if (!Number.isFinite(ts)) return '';
  // 时钟漂移（浏览器比服务端慢一点）会让刚提交的作业算出负数——按刚开始处理，别显示空白
  const ms = Math.max(0, Date.now() - ts);
  const min = Math.floor(ms / 60000);
  if (min < 1) return t('不到 1 分钟');
  if (min < 60) return t('{n} 分钟', { n: min });
  return t('{h} 小时 {m} 分', { h: Math.floor(min / 60), m: min % 60 });
}

/** 台账还没建起来时的阶段说明 —— 只给一个转圈的菊花，用户没法判断是在启动还是已经僵住。 */
function phaseLabel(job: JobBrief): string {
  if (job.status === 'pending') return t('启动中');
  return t('正在建立工作项台账');
}

/** 作业没能善终时的一句话说明（状态条会带着它多留一会儿再消失）。 */
function endedLabel(job: JobBrief): string {
  if (job.status === 'failed') return t('作业失败');
  if (job.status === 'interrupted') return t('作业已失联，可续跑');
  if (job.status === 'cancelled') return t('作业已取消');
  return '';
}

export function JobProgressStrip({ chatId }: { chatId: string }) {
  const [jobs, setJobs] = useState<JobBrief[]>([]);
  const [ended, setEnded] = useState<JobBrief[]>([]);
  const [cancelling, setCancelling] = useState<string>('');
  // 轮询在 effect 外也要能读到最新会话，避免切会话后旧定时器把旧数据写回来
  const chatRef = useRef(chatId);
  chatRef.current = chatId;
  // 见过的在跑作业。列表接口只给未结束的，作业一旦进终态就直接从列表里消失——
  // 没有这份记录，失败的作业就是「状态条忽然不见了」，用户永远不知道它是跑完了还是崩了。
  const seenRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    seenRef.current = new Set();
    setEnded([]);
    if (!chatId) {
      setJobs([]);
      return;
    }
    let alive = true;
    let timer: number | undefined;

    // 刷新/切回会话后补一次终态回捞。`ended` 只活在组件 state 里，重新挂载就清零——
    // 作业要是在页面不在的时候崩了，用户回来看到的是一条**什么都没有**的输入框，
    // 和"作业压根没跑过"完全无法区分。这一发把最近没善终的作业捞回来补上告警。
    const seedEnded = async () => {
      try {
        const all = await listChatJobs(chatId, false);
        if (!alive || chatRef.current !== chatId) return;
        const cutoff = Date.now() - ENDED_LOOKBACK_MS;
        const stale = all.filter((j) => {
          if (j.status === 'completed' || j.status === 'pending' || j.status === 'running') return false;
          const ts = parseServerTime(j.completed_at || j.created_at);
          return Number.isFinite(ts) && ts >= cutoff;
        });
        if (stale.length === 0) return;
        setEnded((prev) => {
          const known = new Set(prev.map((j) => j.job_id));
          return [...prev, ...stale.filter((j) => !known.has(j.job_id))];
        });
      } catch {
        // 回捞失败就算了：轮询照跑，少一条历史告警比卡住状态条好
      }
    };
    void seedEnded();

    const tick = async () => {
      try {
        const rows = await listChatJobs(chatId);
        if (!alive || chatRef.current !== chatId) return;
        setJobs(rows);

        const liveIds = new Set(rows.map((r) => r.job_id));
        const vanished = [...seenRef.current].filter((id) => !liveIds.has(id));
        seenRef.current = liveIds;
        // 消失的作业补查一次终态：跑完了就安静收场，没善终就把原因摆到台面上
        for (const id of vanished) {
          try {
            const final = await getJobApi(id);
            if (!alive || chatRef.current !== chatId) return;
            if (final.status !== 'completed') {
              setEnded((prev) => (prev.some((j) => j.job_id === id) ? prev : [...prev, final]));
            }
          } catch {
            // 查不到就算了：宁可少说一句，也不要编一个结局
          }
        }
      } catch {
        // 轮询失败保持上一帧：状态条抖成空白比慢一拍更糟
      }
      if (alive) timer = window.setTimeout(tick, POLL_MS);
    };
    void tick();

    return () => {
      alive = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [chatId]);

  const onCancel = async (jobId: string) => {
    setCancelling(jobId);
    try {
      await cancelJobApi(jobId);
      setJobs((prev) => prev.filter((j) => j.job_id !== jobId));
    } catch {
      // 取消失败就让下一轮轮询把真实状态刷回来
    } finally {
      setCancelling('');
    }
  };

  return (
    <AnimatePresence initial={false}>
      {jobs.map((job) => {
        const s = job.stats || ({} as JobBrief['stats']);
        const total = s.total ?? 0;
        const settled = s.settled ?? 0;
        const pct = total > 0 ? Math.floor((settled * 100) / total) : 0;
        const elapsed = elapsedLabel(job.started_at || job.created_at);
        return (
          <motion.div
            key={job.job_id}
            className="jx-jobStrip"
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 6, transition: { duration: DUR.fast, ease: EASE.exit } }}
            transition={{ duration: DUR.normal, ease: EASE.brandOut }}
          >
            {total > 0
              ? <ProgressRing settled={settled} total={total} />
              : <LoadingOutlined spin className="jx-jobStrip-spin" />}

            <span className="jx-jobStrip-badge">{t('后台作业')}</span>
            <span className="jx-jobStrip-title">{job.name || t('批量作业')}</span>

            {total > 0 ? (
              <>
                <span className="jx-jobStrip-sep" aria-hidden>·</span>
                <span className="jx-jobStrip-count">{settled}/{total}（{pct}%）</span>
              </>
            ) : (
              /* 台账还是空的：说清楚现在在哪一步，别让用户对着一个菊花猜 */
              <>
                <span className="jx-jobStrip-sep" aria-hidden>·</span>
                <span className="jx-jobStrip-count">{phaseLabel(job)}</span>
              </>
            )}
            {/* 失败数是最该被看见的数：它决定用户要不要现在就叫停 */}
            {s.failed > 0 && (
              <>
                <span className="jx-jobStrip-sep" aria-hidden>·</span>
                <span className="jx-jobStrip-fail">{t('失败 {n}', { n: s.failed })}</span>
              </>
            )}
            {elapsed && (
              <>
                <span className="jx-jobStrip-sep" aria-hidden>·</span>
                <span className="jx-jobStrip-elapsed">{t('已运行 {d}', { d: elapsed })}</span>
              </>
            )}

            <button
              type="button"
              className="jx-jobStrip-cancel"
              disabled={cancelling === job.job_id}
              onClick={() => void onCancel(job.job_id)}
              aria-label={t('取消后台作业')}
              title={t('取消后台作业')}
            >
              <StopOutlined />
            </button>
          </motion.div>
        );
      })}

      {/* 没善终的作业：不能就这么无声消失——用户得知道它停了、停在哪、怎么接着跑 */}
      {ended.map((job) => {
        const s = job.stats || ({} as JobBrief['stats']);
        const total = s.total ?? 0;
        const settled = s.settled ?? 0;
        return (
          <motion.div
            key={`ended-${job.job_id}`}
            className="jx-jobStrip jx-jobStrip--ended"
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 6, transition: { duration: DUR.fast, ease: EASE.exit } }}
            transition={{ duration: DUR.normal, ease: EASE.brandOut }}
          >
            <WarningOutlined className="jx-jobStrip-warn" />
            <span className="jx-jobStrip-badge">{t('后台作业')}</span>
            <span className="jx-jobStrip-title">{job.name || t('批量作业')}</span>
            <span className="jx-jobStrip-sep" aria-hidden>·</span>
            <span className="jx-jobStrip-fail">{endedLabel(job)}</span>
            {total > 0 && (
              <>
                <span className="jx-jobStrip-sep" aria-hidden>·</span>
                <span className="jx-jobStrip-count">{t('已完成 {n}/{m}', { n: settled, m: total })}</span>
              </>
            )}
            {job.error && <span className="jx-jobStrip-reason" title={job.error}>{job.error}</span>}

            <button
              type="button"
              className="jx-jobStrip-cancel"
              onClick={() => setEnded((prev) => prev.filter((j) => j.job_id !== job.job_id))}
              aria-label={t('知道了')}
              title={t('知道了')}
            >
              <CloseOutlined />
            </button>
          </motion.div>
        );
      })}
    </AnimatePresence>
  );
}
