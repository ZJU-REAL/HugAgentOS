/** Markdown body (policy text, generated reports, release notes). */

import { t } from '../../../i18n';
import { mdToHtml } from '../../../utils/markdown';
import { readText } from '../../pointer';
import type { ViewProps } from '../../ViewProps';

export function MarkdownView({ data, map }: ViewProps) {
  const text = readText(data, map.text ?? '$');
  if (!text.trim()) return <div className="jx-pv-empty">{t('暂无内容')}</div>;
  return <div className="jx-md jx-pv-markdown" dangerouslySetInnerHTML={{ __html: mdToHtml(text) }} />;
}
