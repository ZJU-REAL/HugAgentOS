// Change this to switch the timezone used for time display site-wide. The backend returns ISO 8601 UTC; formatting is done uniformly on the frontend.
export const APP_TIMEZONE = 'Asia/Shanghai';

export function pad2(value: number | string) {
  return String(value).padStart(2, '0');
}

function toDate(value?: string | number | Date | null): Date | null {
  if (value === null || value === undefined || value === '') return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

interface ZonedParts {
  year: string;
  month: string;
  day: string;
  hour: string;
  minute: string;
  second: string;
}

const partsFormatter = new Intl.DateTimeFormat('en-CA', {
  timeZone: APP_TIMEZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
});

function zonedParts(value?: string | number | Date | null): ZonedParts | null {
  const date = toDate(value);
  if (!date) return null;
  const out: Partial<ZonedParts> = {};
  for (const p of partsFormatter.formatToParts(date)) {
    if (p.type !== 'literal') (out as Record<string, string>)[p.type] = p.value;
  }
  return {
    year: out.year ?? '0000',
    month: out.month ?? '00',
    day: out.day ?? '00',
    hour: out.hour ?? '00',
    minute: out.minute ?? '00',
    second: out.second ?? '00',
  };
}

/** YYYY/MM/DD HH:MM:SS */
export function formatDateTime(value?: string | number | Date | null, fallback = '-') {
  const p = zonedParts(value);
  return p ? `${p.year}/${p.month}/${p.day} ${p.hour}:${p.minute}:${p.second}` : fallback;
}

/** YYYY/MM/DD */
export function formatDate(value?: string | number | Date | null, fallback = '-') {
  const p = zonedParts(value);
  return p ? `${p.year}/${p.month}/${p.day}` : fallback;
}

/** HH:MM:SS */
export function formatTime(value?: string | number | Date | null, fallback = '-') {
  const p = zonedParts(value);
  return p ? `${p.hour}:${p.minute}:${p.second}` : fallback;
}

/** MM.DD */
export function formatMonthDay(value?: string | number | Date | null, fallback = '-') {
  const p = zonedParts(value);
  return p ? `${p.month}.${p.day}` : fallback;
}

/** MM/DD HH:MM */
export function formatShortDateTime(value?: string | number | Date | null, fallback = '-') {
  const p = zonedParts(value);
  return p ? `${p.month}/${p.day} ${p.hour}:${p.minute}` : fallback;
}

/** MM/DD HH:MM:SS */
export function formatMonthDayTime(value?: string | number | Date | null, fallback = '-') {
  const p = zonedParts(value);
  return p ? `${p.month}/${p.day} ${p.hour}:${p.minute}:${p.second}` : fallback;
}

/** YYYY.MM.DD HH:MM:SS */
export function formatFullDateTime(value?: string | number | Date | null, fallback = '-') {
  const p = zonedParts(value);
  return p ? `${p.year}.${p.month}.${p.day} ${p.hour}:${p.minute}:${p.second}` : fallback;
}

/** YYYY-MM-DD, used for filenames / grouping keys */
export function formatDateKey(value?: string | number | Date | null, fallback = '') {
  const p = zonedParts(value);
  return p ? `${p.year}-${p.month}-${p.day}` : fallback;
}
