import type { AutomationRunStatus } from '../../types';
import type { ChannelConversation } from '../../api';
import { t } from '../../i18n';

/** A distinguishable label for a channel conversation: bot name · group/direct chat · the real Feishu conversation ID.
 *  Don't use the first message content (title, e.g. "hello") -- it can collide and can't tell conversations apart. */
export function channelConversationLabel(c: ChannelConversation): string {
  const kind = c.chat_type === 'group' ? t('群') : t('单聊');
  const head = c.bot_name ? `${c.bot_name} · ` : '';
  return `${head}${kind} · ${c.conversation_id}`;
}

/** Labels for a single automation run's status. Kept here so they aren't redeclared across multiple components. */
export const RUN_STATUS_LABEL: Record<AutomationRunStatus, string> = {
  running: t('执行中'),
  success: t('成功'),
  failed: t('失败'),
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

/** Cron preset options for the UI. */
export const CRON_PRESETS = [
  { label: t('每天 09:00'), value: '0 9 * * *' },
  { label: t('工作日 09:00'), value: '0 9 * * 1-5' },
  { label: t('每周一 09:00'), value: '0 9 * * 1' },
  { label: t('每小时'), value: '0 * * * *' },
  { label: t('每 2 小时'), value: '0 */2 * * *' },
  { label: t('每 6 小时'), value: '0 */6 * * *' },
  { label: t('自定义'), value: '' },
];
