import type { MenuProps } from 'antd';
import type { ApplicationEntry } from '../applicationEntries';
import { t } from '../i18n';
import { ApplicationIcon } from './ApplicationIcon';

export function applicationMenuItems(entries: ApplicationEntry[], onOpen: (entry: ApplicationEntry) => void): MenuProps['items'] {
  return entries.map(entry => ({
    key: entry.key,
    label: entry.title,
    icon: <ApplicationIcon icon={entry.icon} builtinIcon={entry.builtinIcon} />,
    disabled: !entry.item.availability.available,
    title: entry.item.availability.reason ? t(entry.item.availability.reason) : undefined,
    onClick: () => onOpen(entry),
  }));
}
