import dayjs, { type Dayjs } from 'dayjs';
import timezonePlugin from 'dayjs/plugin/timezone';
import utc from 'dayjs/plugin/utc';
import type { AutomationScheduleType } from '../../types';
import { currentTimezone } from './automationLocation';
dayjs.extend(utc);
dayjs.extend(timezonePlugin);

// Picker values are wall-clock fields in the task's zone, not the browser's.
export function wallNow(zone = currentTimezone()): Dayjs {
  return dayjs(dayjs().tz(zone).format('YYYY-MM-DDTHH:mm:ss'));
}

export interface ScheduleValue {
  schedule_type: AutomationScheduleType;
  cron_expression: string;
}

/**
 * 单次执行时间是否落在过去——提交前校验用，导出给创建 / 编辑两处复用。
 *
 * 注意不能拿 parseOnceCron 的结果去比：它已经把过期日期顺延到明年，永远是未来。
 * 这里要判断的恰恰是「本年度的那个时刻已经过去了」，因为此时 cron 要等一年才会再匹配，
 * 用户以为是马上执行、实际却是明年的今天。
 */
export function isOnceScheduleExpired(value: ScheduleValue, zone?: string): boolean {
  if (value.schedule_type !== 'once') return false;
  const parts = value.cron_expression.trim().split(/\s+/);
  if (parts.length !== 5) return false;
  const [m, h, d, mo] = parts.map((p) => parseInt(p, 10));
  if ([m, h, d, mo].some((n) => Number.isNaN(n))) return false;
  const thisYear = wallNow(zone).month(mo - 1).date(d).hour(h).minute(m).second(0);
  return thisYear.isBefore(wallNow(zone));
}
