/** One-line completion badge — the lightest possible tool result. */

import { CheckCircleOutlined } from '@ant-design/icons';

import { t } from '../../../i18n';
import { readText } from '../../pointer';
import type { ViewProps } from '../../ViewProps';

export function BadgeView({ data, map }: ViewProps) {
  const text = readText(data, map.text) || t('工具执行完成');
  return (
    <div className="jx-pv-badge">
      <CheckCircleOutlined className="jx-pv-badgeIcon" />
      <span>{text}</span>
    </div>
  );
}
