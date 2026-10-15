import { PersonalAgentNavStatus, usePersonalAgentStatusText } from '../personalAgentEdition';
import {activityAccessibleName,useApplicationActivity} from './activityBadge';
import { Badge, Tooltip } from 'antd';
import type { ApplicationEntry } from '../applicationEntries';
import { ApplicationIcon } from './ApplicationIcon';
export function ApplicationButton({entry,onOpen,current,className,expanded = false,count = 0}: {
  entry: ApplicationEntry; onOpen: (entry: ApplicationEntry) => void; current: boolean; className: string;
  expanded?: boolean; count?: number;
}) {
  const activity=useApplicationActivity(entry.key,count);
  const personalStatus=usePersonalAgentStatusText();
  const icon = <ApplicationIcon icon={entry.icon} builtinIcon={entry.builtinIcon} />;
  const button = <button type="button" data-rail-application={entry.key} className={className + (current ? ' active' : '')}
    aria-label={[activityAccessibleName(entry.title,activity.count,activity.background),entry.key==='builtin:personal_agent'?personalStatus:''].filter(Boolean).join(' · ')} aria-current={current ? 'page' : undefined} aria-pressed={current}
    disabled={!entry.item.availability.available} onClick={() => onOpen(entry)}>
    {activity.count > 0 ? <Badge count={activity.count} size="small">{icon}</Badge> : icon}{activity.background && <span aria-hidden="true">●</span>}{expanded && <span>{entry.title}</span>}{entry.key === 'builtin:personal_agent' && <PersonalAgentNavStatus compact={!expanded} />}
  </button>;
  return expanded ? button : <Tooltip title={[entry.title, entry.source].filter(Boolean).join(' · ')} placement="right">{button}</Tooltip>;
}
