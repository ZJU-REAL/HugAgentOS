/** Evidence-anchor chip.
 *
 * The backend's citation middleware stamps `cite_id` onto individual result
 * entries; showing it lets a reader match `[anchor](cite:e7)` in the answer to
 * the exact row in the tool card.
 */

import { t } from '../../../i18n';

export function CiteTag({ item }: { item: unknown }) {
  const id = item && typeof item === 'object' ? String((item as Record<string, unknown>).cite_id || '') : '';
  if (!/^e\d+$/.test(id)) return null;
  return <span className="jx-pv-cite" title={t('引用锚点 {id}', { id })}>{id}</span>;
}
