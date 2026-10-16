import './activity.css';
import { activityAccessibleName, useApplicationActivity } from './activityBadge';

export function ApplicationBadge({applicationKey, count = 0}: {applicationKey: string; count?: number}) {
  const value = useApplicationActivity(applicationKey, count);
  if (!value.count && !value.background) return null;
  return <span aria-hidden="true" className="jx-applicationBadge">
    {value.count > 0 && <span>{value.count > 99 ? '99+' : value.count}</span>}
    {value.background && <span>●</span>}
  </span>;
}

export function ApplicationActivityStatus({applicationKey}: {applicationKey: string}) {
  const value = useApplicationActivity(applicationKey);
  return <span style={{position: 'absolute', width: 1, height: 1, overflow: 'hidden',
    clipPath: 'inset(50%)'}}>{activityAccessibleName('', value.count, value.background)}</span>;
}
