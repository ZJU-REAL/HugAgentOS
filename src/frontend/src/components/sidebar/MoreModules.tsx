import { useRef, useState } from 'react';
import { EllipsisOutlined } from '@ant-design/icons';
import { Dropdown } from 'antd';
import type { PanelKey } from '../../types';
import { t } from '../../i18n';
import type { ModuleEntry } from './moduleNavigation';
import type { SidebarProps } from './sidebarTypes';

export function MoreModules({ entries, panel, onSelect, expanded = false, className = 'jx-moduleButton' }: {
  entries: readonly ModuleEntry[]; panel: PanelKey; onSelect: SidebarProps['onSetPanel']; expanded?: boolean; className?: string;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  if (!entries.length) return null;
  return <Dropdown open={open} onOpenChange={visible => {
    setOpen(visible);
    if (!visible) requestAnimationFrame(() => trigger.current?.focus());
  }} overlayClassName="jx-moreModulesMenu" autoFocus trigger={['click']} placement="bottomLeft"
    popupRender={menu => <div onKeyDownCapture={event => {
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus();
      }
    }}>{menu}</div>}
    menu={{ selectable: true, selectedKeys: [panel], items: entries.map(({ id, meta }) => ({
      key: id, label: meta.label, icon: <img className="jx-moduleMenuIcon" src={meta.icon} alt="" />,
      onClick: () => { setOpen(false); onSelect(meta.targetPanel); },
    })) }}>
    <button ref={trigger} type="button" className={className + (entries.some(e => e.meta.targetPanel === panel) ? ' active' : '')}
      aria-label={t('更多')} aria-haspopup="menu" aria-expanded={open} onKeyDown={event => {
        if (['Enter', ' ', 'ArrowDown'].includes(event.key)) {
          event.preventDefault(); event.stopPropagation(); setOpen(true);
        }
      }}><EllipsisOutlined />{expanded && <span>{t('更多')}</span>}</button>
  </Dropdown>;
}
