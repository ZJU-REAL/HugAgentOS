import { useState } from 'react';
import type { ApplicationEntry } from '../../applicationEntries';
import { usePageConfigAll } from '../../hooks/usePageConfig';
import { t } from '../../i18n';
import { abilitySlug, useAbilityTab } from '../../routing/subPages';
import { ApplicationButton } from '../../workbench/ApplicationButton';
import { ApplicationIcon } from '../../workbench/ApplicationIcon';
import { LAYOUT_ITEMS } from './items';

export function ClassicNavigation({ entries, pinned, currentKey, expanded, count, onOpen }: {
  entries: ApplicationEntry[];
  pinned: string[];
  currentKey: string;
  expanded: boolean;
  count: number;
  onOpen: (entry: ApplicationEntry, sub?: string) => void;
}) {
  const [groups, setGroups] = useState<Record<string, boolean>>({});
  const tab = useAbilityTab();
  const config = usePageConfigAll();
  return <nav className={expanded ? 'jx-classicNav' : 'jx-classicMiniNav'} aria-label={t('应用导航')}>
    {pinned.map(key => {
      const entry = entries.find(item => item.key === key);
      if (!entry) return null;
      const children = key.startsWith('builtin:') ? LAYOUT_ITEMS[entry.item.declaration.id]?.children : undefined;
      if (!expanded || !children?.length) return <ApplicationButton key={key} entry={entry} onOpen={onOpen}
        current={key === currentKey} expanded={expanded} count={key === 'builtin:my_space' ? count : 0}
        className={expanded ? 'jx-navItem' : 'jx-miniRailBtn'} />;
      const collapsed = !!groups[key];
      const active = key === currentKey;
      return <div key={key} className="jx-classicNavGroup">
        <button type="button" data-rail-application={key} className={'jx-navItem' + (active && collapsed ? ' active' : '')}
          aria-label={entry.title} aria-expanded={!collapsed} aria-controls="classic-ability-categories"
          disabled={!entry.item.availability.available}
          onClick={() => {
            setGroups(previous => ({ ...previous, [key]: !collapsed }));
            if (collapsed) onOpen(entry);
          }}>
          <ApplicationIcon icon={entry.icon} builtinIcon={entry.builtinIcon} /><span>{entry.title}</span>
        </button>
        {!collapsed && <div id="classic-ability-categories" className="jx-classicSubList">
          {children.map(child => <button type="button" key={child.key}
            className={'jx-msRail-subItem' + (active && tab === child.key ? ' active' : '')}
            aria-current={active && tab === child.key ? 'page' : undefined}
            disabled={!entry.item.availability.available}
            onClick={() => onOpen(entry, abilitySlug(child.key))}>
            {t(config.navigation.panel_titles[child.key] || child.label)}
          </button>)}
        </div>}
      </div>;
    })}
  </nav>;
}
