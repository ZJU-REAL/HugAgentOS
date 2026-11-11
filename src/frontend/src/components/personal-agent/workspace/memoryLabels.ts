import { t } from '../../../i18n';

/** Page types in the order the index groups them; labels match personal_agent/memory/layout.py. */
export const PAGE_TYPES: { key: string; label: string }[] = [
  { key: 'person', label: '人' },
  { key: 'org', label: '组织' },
  { key: 'project', label: '项目' },
  { key: 'topic', label: '主题与决定' },
  { key: 'procedure', label: '做法' },
];

export function typeLabel(type: string): string {
  const found = PAGE_TYPES.find((item) => item.key === type);
  return found ? t(found.label) : type;
}

const WRITERS: Record<string, string> = {
  online: '智能体（干活时）',
  analyzer: 'Dreaming 分析',
  organizer: 'Dreaming 整理',
  user: '你',
  system: '系统',
};

export const WRITER_KEYS = Object.keys(WRITERS);

export function writerLabel(writer: string): string {
  return WRITERS[writer] ? t(WRITERS[writer]) : writer;
}

export function formatTime(value?: string | number | null, timeZone?: string): string {
  if (value === null || value === undefined || value === '') return '—';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  try { return date.toLocaleString(undefined, timeZone ? { timeZone } : undefined); }
  catch { return date.toLocaleString(); }
}

/** memory/person-zhang-san.md → person-zhang-san; other files keep their workspace name. */
export function fileLabel(path: string): string {
  const rel = path.replace(/^\/workspace\//, '');
  const page = /^memory\/([a-z0-9-]+)\.md$/.exec(rel);
  if (page && !page[1].startsWith('_')) return page[1];
  return rel;
}

export const DREAMING_STATES: Record<string, string> = {
  pending: '等待整理', running: '正在整理', retry: '等待重试', failed: '整理失败', done: '整理完成',
};
