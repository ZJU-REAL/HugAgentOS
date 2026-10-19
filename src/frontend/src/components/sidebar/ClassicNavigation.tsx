import { useState } from 'react';
import type { ModuleEntry } from './moduleNavigation';
import type { PanelKey } from '../../types';
import type { SidebarProps } from './sidebarTypes';
import { usePageConfigAll } from '../../hooks/usePageConfig';
import { t } from '../../i18n';
import { abilitySlug, useAbilityTab } from '../../routing/subPages';
import { ModuleButton } from './ModuleButton';
import { MoreModules } from './MoreModules';

export function ClassicNavigation({ primary, more, panel, expanded, count, onSelect }: {
  primary: ModuleEntry[];
  more: ModuleEntry[];
  panel: PanelKey;
  expanded: boolean;
  count: number;
  onSelect: SidebarProps['onSetPanel'];
}) {
  const [groups, setGroups] = useState<Record<string, boolean>>({});
  const tab = useAbilityTab();
  const config = usePageConfigAll();
  return <nav className={expanded ? 'jx-classicNav' : 'jx-classicMiniNav'} aria-label={t('应用导航')}>
    {primary.map(entry => {
      const key = entry.id;
      const children = entry.meta.children;
      if (!expanded || !children?.length) return <ModuleButton key={key} entry={entry} onSelect={onSelect}
        active={panel === entry.meta.targetPanel} expanded={expanded} count={key === 'my_space' ? count : 0}
        className={expanded ? 'jx-navItem' : 'jx-miniRailBtn'} />;
      const collapsed = !!groups[key];
      const active = panel === entry.meta.targetPanel;
      return <div key={key} className="jx-classicNavGroup">
        <button type="button" className={'jx-navItem' + (active && collapsed ? ' active' : '')}
          aria-label={entry.meta.label} aria-expanded={!collapsed} aria-controls="classic-ability-categories"
          onClick={() => {
            setGroups(previous => ({ ...previous, [key]: !collapsed }));
            if (collapsed) onSelect(entry.meta.targetPanel);
          }}>
          <img className="jx-navItemIcon" src={entry.meta.icon} alt="" /><span>{entry.meta.label}</span>
        </button>
        {!collapsed && <div id="classic-ability-categories" className="jx-classicSubList">
          {children.map(child => <button type="button" key={child.key}
            className={'jx-msRail-subItem' + (active && tab === child.key ? ' active' : '')}
            aria-current={active && tab === child.key ? 'page' : undefined}
            onClick={() => onSelect(entry.meta.targetPanel, abilitySlug(child.key))}>
            {t(config.navigation.panel_titles[child.key] || child.label)}
          </button>)}
        </div>}
      </div>;
    })}
    <MoreModules entries={more} panel={panel} onSelect={onSelect} expanded={expanded} className={expanded ? 'jx-navItem' : 'jx-miniRailBtn'} />
  </nav>;
}
