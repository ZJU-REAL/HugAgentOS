import type { ChannelConversation } from '../../api';
import { t } from '../../i18n';
import type { AutomationRun,AutomationRunStatus } from '../../types';

/** A distinguishable label for a channel conversation: bot name · group/direct chat · the real Feishu conversation ID.
 *  Don't use the first message content (title, e.g. "hello") -- it can collide and can't tell conversations apart. */
export function channelConversationLabel(c: ChannelConversation): string {
  const kind = c.chat_type === 'group' ? t('群') : t('单聊');
  const head = c.bot_name ? `${c.bot_name} · ` : '';
  return `${head}${kind} · ${c.conversation_id}`;
}

/** Labels for a single automation run's status. Kept here so they aren't redeclared across multiple components. */
export const RUN_STATUS_LABEL: Record<AutomationRunStatus, string> = {
  pending: t('待执行'),
  running: t('执行中'),
  success: t('成功'),
  failed: t('失败'),
};

/** Queued or executing — the run has no final result yet. */
export function isRunActive(run: Pick<AutomationRun, 'status'>): boolean {
  return run.status === 'running' || run.status === 'pending';
}

// First, second … tenth; above 10 use Arabic numerals
const CN_DIGITS = ['', '一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];
export function formatRunOrdinal(n: number): string {
  return `第${CN_DIGITS[n] || n}次`;
}

/** One-line result of a run: its summary, its error, or what it is waiting for. */
export function summarizeRun(run: AutomationRun): string {
  if (isRunActive(run)) return t('任务正在执行，结果生成后会自动更新到对话区。');
  if (run.status === 'failed') {
    return String(run.error_message || run.result_summary || t('执行失败，请进入详情查看错误信息。')).replace(/\s+/g, ' ').trim();
  }
  return String(run.result_summary || t('执行完成，可进入对话查看完整输出与附件结果。')).replace(/\s+/g, ' ').trim();
}

/** Status modifier classes for the run dot (`.jx-automation-runDot`), shared by the task
 *  detail page and the aggregated run list. The running state additionally carries the
 *  `.jx-anim-ripple` motion primitive; colour/spread live in automation.css. */
export const RUN_STATUS_CLASS: Record<AutomationRunStatus, string> = {
  pending: 'is-running',
  running: 'is-running jx-anim-ripple',
  success: 'is-success',
  failed: 'is-failed',
};

/** Convert a 5-field cron expression to a human-readable Chinese string. */
export function cronToHumanReadable(cron: string): string {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return cron;
  const [minute, hour, , , dayOfWeek] = parts;

  const timeStr = `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`;

  const DOW_MAP: Record<string, string> = {
    '1': t('周一'), '2': t('周二'), '3': t('周三'), '4': t('周四'),
    '5': t('周五'), '6': t('周六'), '0': t('周日'), '7': t('周日'),
  };

  // Every N hours
  if (hour.startsWith('*/')) {
    const n = hour.slice(2);
    return t('每 {n} 小时', { n });
  }
  // Every N minutes
  if (minute.startsWith('*/')) {
    const n = minute.slice(2);
    return t('每 {n} 分钟', { n });
  }

  // Specific day of week
  if (dayOfWeek === '1-5') return t('工作日 {time}', { time: timeStr });
  if (dayOfWeek === '*') return t('每天 {time}', { time: timeStr });
  if (/^\d$/.test(dayOfWeek)) return t('每{day} {time}', { day: DOW_MAP[dayOfWeek] || dayOfWeek, time: timeStr });

  return `${cron} (${t('自定义')})`;
}

/**
 * 执行耗时的统一中文格式：秒级只显示秒，进到分钟就「X分Y秒」，进到小时就「X小时Y分」。
 * 原先详情页 / 列表 / 时间线各写各的（`12.3s`、`740 秒`、`3m 05s`），同一条记录在三个
 * 地方三种写法，且 740 秒这种读起来要心算。这里做成唯一真源，三处都引它。
 */
export function formatRunDuration(durationMs?: number | null): string {
  if (!durationMs || durationMs <= 0) return '-';
  const totalSeconds = Math.round(durationMs / 1000);
  if (totalSeconds < 1) return t('不到 1 秒');
  if (totalSeconds < 60) return t('{s} 秒', { s: totalSeconds });

  const totalMinutes = Math.floor(totalSeconds / 60);
  if (totalMinutes < 60) {
    const s = totalSeconds % 60;
    return s > 0 ? t('{m} 分 {s} 秒', { m: totalMinutes, s }) : t('{m} 分', { m: totalMinutes });
  }

  const hours = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return m > 0 ? t('{h} 小时 {m} 分', { h: hours, m }) : t('{h} 小时', { h: hours });
}

/** Format ISO date string to relative time (e.g., "in 2 hours"). */
export function formatRelativeTime(isoStr: string): string {
  const target = new Date(isoStr).getTime();
  const now = Date.now();
  const diffMs = target - now;

  if (diffMs < 0) return t('已过期');

  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return t('即将执行');
  if (minutes < 60) return t('{n} 分钟后', { n: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t('{n} 小时后', { n: hours });
  const days = Math.floor(hours / 24);
  return t('{n} 天后', { n: days });
}

/**
 * IANA 时区标识 → 中文可读名。任务详情原样显示 `Asia/Shanghai`，非技术用户读不懂。
 * 只覆盖后端实际会下发的少数几个；未知值退回原串，不至于把信息弄丢。
 */
const TIMEZONE_LABEL: Record<string, string> = {
  'Asia/Shanghai': t('中国标准时间（UTC+8）'),
  'Asia/Hong_Kong': t('香港时间（UTC+8）'),
  'Asia/Taipei': t('台北时间（UTC+8）'),
  'Asia/Tokyo': t('日本标准时间（UTC+9）'),
  'Asia/Singapore': t('新加坡时间（UTC+8）'),
  UTC: t('协调世界时（UTC）'),
};

export function formatTimezone(tz?: string | null): string {
  if (!tz) return TIMEZONE_LABEL['Asia/Shanghai'];
  return TIMEZONE_LABEL[tz] || tz;
}

export const SCHEDULE_TYPE_LABEL = { recurring: t('周期执行'), once: t('单次执行'), manual: t('手动执行') };
