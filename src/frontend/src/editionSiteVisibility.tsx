import type { ReactElement } from 'react';

import { t } from './i18n';

export type SiteVisibility = 'public' | 'private';

export interface SiteEditionFields {
  readonly __enterpriseSiteFields?: never;
}

export interface SiteUpdateEditionFields {
  readonly __enterpriseSiteUpdateFields?: never;
}

export function normalizeSiteVisibility(raw: unknown): SiteVisibility {
  return raw === 'private' ? 'private' : 'public';
}

export function normalizeSiteEditionFields(_raw: Record<string, unknown>): SiteEditionFields {
  return {};
}

export function editionSiteFormValues(_site: SiteEditionFields): Record<string, unknown> {
  return {};
}

export function editionSiteUpdateFields(
  _visibility: SiteVisibility,
  _values: Record<string, unknown>,
): SiteUpdateEditionFields {
  return {};
}

export function getSiteVisibilityOptions(): Array<{ value: SiteVisibility; label: string }> {
  return [
    { value: 'public', label: t('公开 — 任何人凭链接访问') },
    { value: 'private', label: t('私密 — 仅自己登录后可见') },
  ];
}

export function getSiteVisibilityTag(visibility: SiteVisibility): { color?: string; label: string } {
  return visibility === 'public'
    ? { color: 'blue', label: t('公开') }
    : { label: t('私密') };
}

export function EditionSiteVisibilityTag(
  _props: { visibility: SiteVisibility },
): ReactElement | null {
  return null;
}

export function EditionSiteVisibilityFields(
  _props: { visibility: SiteVisibility },
): ReactElement | null {
  return null;
}
