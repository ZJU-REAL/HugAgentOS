/** Leaderboard: rank, subject, score bar, movement — with optional drill-down.
 *
 * Keeps the ordinal meaning that a plain list loses: the reader sees who leads
 * and by how much, not just a sequence of rows.
 */

import { t } from '../../../i18n';
import { readNumber, readRecords, readText } from '../../pointer';
import { usableActions, type ViewProps } from '../../ViewProps';
import { InlineActions } from '../shared/InlineActions';
import { formatNumber } from '../svg/primitives';

export function RankingView({ data, map, actions, ctx }: ViewProps) {
  const items = readRecords(data, map.items);
  if (items.length === 0) return <div className="jx-pv-empty">{t('暂无排名数据')}</div>;
  const itemActions = usableActions(actions, 'item', ctx.toolName);

  const scores = items.map((item) => readNumber(item, map.score) ?? 0);
  const maxScore = Math.max(...scores, 1);

  return (
    <ol className="jx-pv-ranking">
      {items.map((item, index) => {
        const rank = readNumber(item, map.rank) ?? index + 1;
        const name = readText(item, map.name) || t('第 {n} 名', { n: rank });
        const score = scores[index];
        const trend = readText(item, map.trend);
        const rising = /^\+|升|↑/.test(trend);
        const falling = /^-|降|↓/.test(trend);
        return (
          <li className="jx-pv-rankRow" key={`${name}-${index}`}>
            <span className={`jx-pv-rankNo${rank <= 3 ? ' top' : ''}`}>{rank}</span>
            <span className="jx-pv-rankName" title={name}>{name}</span>
            <span className="jx-pv-rankTrack">
              <span className="jx-pv-rankFill" style={{ width: `${Math.max(2, (score / maxScore) * 100)}%` }} />
            </span>
            {score > 0 && <span className="jx-pv-rankScore">{formatNumber(score)}</span>}
            {trend && (
              <span className={`jx-pv-rankTrend${rising ? ' up' : falling ? ' down' : ''}`}>{trend}</span>
            )}
            <InlineActions actions={itemActions} item={item} ctx={ctx} />
          </li>
        );
      })}
    </ol>
  );
}
