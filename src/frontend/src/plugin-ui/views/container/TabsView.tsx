/**
 * The one container view: composes other views into tabs.
 *
 * Without it a plugin could only ever pick a single shape per tool, so a result
 * that legitimately contains headline metrics *and* a ranking *and* a trend had
 * to be flattened into whichever one lost the least. Child specs are rendered
 * through the host's renderer, and nesting is capped by the contract so a
 * manifest cannot build an unbounded render tree.
 */

import { useState } from 'react';

import { t } from '../../../i18n';
import { resolveText } from '../../i18n';
import type { ViewProps } from '../../ViewProps';
import type { ChildViewSpec } from '../../types';

export function TabsView({ data, map, ctx }: ViewProps) {
  const specs = (Array.isArray(map.tabs) ? map.tabs : []) as ChildViewSpec[];
  const usable = specs.filter((spec) => spec && spec.view);
  const [active, setActive] = useState(0);

  if (usable.length === 0 || !ctx.renderChild) {
    return <div className="jx-pv-empty">{t('暂无内容')}</div>;
  }

  const current = usable[Math.min(active, usable.length - 1)];

  return (
    <div className="jx-pv-tabs">
      <div className="jx-pv-tabBar" role="tablist">
        {usable.map((spec, index) => (
          <button
            type="button"
            key={index}
            role="tab"
            aria-selected={index === active}
            className={`jx-pv-tab${index === active ? ' is-active' : ''}`}
            onClick={() => setActive(index)}
          >
            {resolveText(spec.label, t('第 {n} 页', { n: index + 1 }))}
          </button>
        ))}
      </div>
      <div className="jx-pv-tabPanel" role="tabpanel">
        {ctx.renderChild(
          { view: current.view!, map: current.map, actions: current.actions },
          data,
        )}
      </div>
    </div>
  );
}
