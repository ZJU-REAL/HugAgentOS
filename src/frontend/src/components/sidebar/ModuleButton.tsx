import { Badge, Tooltip } from 'antd';
import type { ModuleEntry } from './moduleNavigation';
import type { SidebarProps } from './sidebarTypes';

export function ModuleButton({ entry, active, onSelect, expanded = false, count = 0, className }: {
  entry: ModuleEntry; active: boolean; onSelect: SidebarProps['onSetPanel'];
  expanded?: boolean; count?: number; className: string;
}) {
  const { meta } = entry;
  return <Tooltip title={expanded ? undefined : meta.label} placement="right">
    <button type="button" className={className + (active ? ' active' : '')}
      aria-label={meta.label} aria-current={active ? 'page' : undefined}
      onClick={() => onSelect(meta.targetPanel)}>
      <Badge count={count} size="small"><img className={expanded ? 'jx-navItemIcon' : 'jx-miniRailIcon'} src={meta.icon} alt="" /></Badge>
      {expanded && <span>{meta.label}</span>}
    </button>
  </Tooltip>;
}
