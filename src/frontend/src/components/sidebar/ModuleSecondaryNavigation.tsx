import type { PanelKey } from '../../types';
import { t } from '../../i18n';
import type { ModuleEntry } from './moduleNavigation';
import type { SidebarProps } from './sidebarTypes';

export function ModuleSecondaryNavigation({ entries, panel, onSelect }: {
  entries: readonly ModuleEntry[]; panel: PanelKey; onSelect: SidebarProps['onSetPanel'];
}) {
  return <nav className="jx-moduleCategories" aria-label={t('导航')}>
    {entries.map(({ id, meta }) => <button key={id} type="button"
      className={'jx-navItem' + (meta.targetPanel === panel ? ' active' : '')}
      aria-current={meta.targetPanel === panel ? 'page' : undefined} onClick={() => onSelect(meta.targetPanel)}>
      <img className="jx-navItemIcon" src={meta.icon} alt="" />{meta.label}
    </button>)}
  </nav>;
}
