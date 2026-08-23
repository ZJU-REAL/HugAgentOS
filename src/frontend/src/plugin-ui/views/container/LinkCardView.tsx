/** External link card — a published site, a fetched page, any outbound URL. */

import { ExportOutlined, LinkOutlined } from '@ant-design/icons';

import { t } from '../../../i18n';
import { readText } from '../../pointer';
import type { ViewProps } from '../../ViewProps';

function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return '';
  }
}

export function LinkCardView({ data, map }: ViewProps) {
  const url = readText(data, map.url);
  if (!url) return <div className="jx-pv-empty">{t('暂无链接')}</div>;
  const title = readText(data, map.title) || hostOf(url) || url;
  const description = readText(data, map.description);

  return (
    <a className="jx-pv-linkCard" href={url} target="_blank" rel="noopener noreferrer">
      <span className="jx-pv-linkIcon"><LinkOutlined /></span>
      <span className="jx-pv-linkBody">
        <strong>{title}</strong>
        {description && <small>{description}</small>}
        <span className="jx-pv-linkHost">{hostOf(url) || url}</span>
      </span>
      <ExportOutlined className="jx-pv-linkGo" />
    </a>
  );
}
