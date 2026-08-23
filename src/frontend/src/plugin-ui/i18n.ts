/**
 * Resolve contributed display text.
 *
 * A plugin may write a plain string or an i18n map. Both are accepted because
 * manifests are authored in Chinese today while the product ships an English
 * locale — a Chinese-only contract would render mixed-language UI for English
 * users. Resolution order: current locale → `zh-CN` → first available value.
 */

import { getLang } from '../i18n';
import type { I18nText } from './types';

export function resolveText(value: I18nText | undefined, fallback = ''): string {
  if (typeof value === 'string') return value || fallback;
  if (!value || typeof value !== 'object') return fallback;
  const lang = getLang();
  const exact = value[lang];
  if (typeof exact === 'string' && exact) return exact;
  const zh = value['zh-CN'];
  if (typeof zh === 'string' && zh) return zh;
  const first = Object.values(value).find((v) => typeof v === 'string' && v);
  return (first as string) || fallback;
}

/** Fill `{input.field}`-style placeholders in a contributed step text. */
export function fillTemplate(text: string, vars: Record<string, unknown>): string {
  if (!text.includes('{')) return text;
  return text.replace(/\{([a-zA-Z0-9_.]{1,64})\}/g, (whole, path: string) => {
    const key = path.startsWith('input.') ? path.slice(6) : path;
    const value = vars[key];
    if (value === undefined || value === null) return whole;
    return typeof value === 'object' ? whole : String(value);
  });
}
