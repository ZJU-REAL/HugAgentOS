/** Icon card grid — the shape marketplace-style tools want (agents, skills, plugins). */

import { t } from '../../../i18n';
import { readRecords, readText } from '../../pointer';
import { usableActions, type ViewProps } from '../../ViewProps';
import { InlineActions } from '../shared/InlineActions';
import { RawValue } from '../shared/RawValue';

export function GalleryView({ data, map, actions, ctx }: ViewProps) {
  const items = readRecords(data, map.items);
  if (items.length === 0) return <div className="jx-pv-empty">{t('暂无条目')}</div>;
  const itemActions = usableActions(actions, 'item', ctx.toolName);

  return (
    <div className="jx-pv-gallery">
      {items.map((item, index) => {
        const title = readText(item, map.title) || t('第 {n} 项', { n: index + 1 });
        const description = readText(item, map.description);
        const icon = readText(item, map.icon);
        const badge = readText(item, map.badge);
        return (
          <div className="jx-pv-card" key={index}>
            <div className="jx-pv-cardHead">
              {icon
                ? <img className="jx-pv-cardIcon" src={icon} alt="" loading="lazy" />
                : <span className="jx-pv-cardIcon jx-pv-cardIcon--text">{title.slice(0, 1)}</span>}
              <button
                type="button"
                className="jx-pv-cardTitle"
                onClick={() => ctx.openDetail?.(title, <RawValue value={item} expanded />)}
              >
                {title}
              </button>
              {badge && <span className="jx-pv-cardBadge">{badge}</span>}
            </div>
            {description && <p className="jx-pv-cardDesc">{description}</p>}
            {itemActions.length > 0 && (
              <div className="jx-pv-cardActions">
                <InlineActions actions={itemActions} item={item} ctx={ctx} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
