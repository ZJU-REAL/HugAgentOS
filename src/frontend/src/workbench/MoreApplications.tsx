import { useRef, useState } from 'react';
import { EllipsisOutlined } from '@ant-design/icons';
import { Dropdown } from 'antd';
import type { ApplicationEntry } from '../applicationEntries';
import { t } from '../i18n';
import { applicationMenuItems } from './applicationMenuItems';
import './navigation.css';

export function MoreApplications({ entries, currentKey, onOpen }: {
  entries: ApplicationEntry[]; currentKey?: string; onOpen: (entry: ApplicationEntry) => void;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  if (!entries.length) return null;
  return <Dropdown open={open} onOpenChange={visible => {
    setOpen(visible);
    if (!visible) requestAnimationFrame(() => trigger.current?.focus());
  }} autoFocus
    popupRender={menu => <div onKeyDownCapture={event => {
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus();
      }
    }}>{menu}</div>}
    menu={{ items: applicationMenuItems(entries, entry => { setOpen(false); onOpen(entry); }),
      selectable: true, selectedKeys: currentKey ? [currentKey] : [] }}
    trigger={['click']} placement="bottomLeft" overlayClassName="jx-moreApplicationsMenu">
    <button ref={trigger} type="button" onKeyDown={event => {
      if (['Enter', ' ', 'ArrowDown'].includes(event.key)) {
        event.preventDefault(); event.stopPropagation(); setOpen(true);
      }
    }} className={'jx-moduleButton' + (entries.some(entry => entry.key === currentKey) ? ' active' : '')}
      aria-label={t('更多')} aria-haspopup="menu" aria-expanded={open}><EllipsisOutlined /></button>
  </Dropdown>;
}
