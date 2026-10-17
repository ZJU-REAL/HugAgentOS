import type { ReactNode } from 'react';

interface SettingGroupProps {
  title: string;
  children: ReactNode;
}

/** A titled card of setting rows. */
export function SettingGroup({ title, children }: SettingGroupProps) {
  return <section className="pa-set-group"><h3>{title}</h3><div className="pa-set-card">{children}</div></section>;
}

interface SettingRowProps {
  icon?: ReactNode;
  label: string;
  description?: ReactNode;
  control?: ReactNode;
  action?: ReactNode;
  /** Wide controls (multi-selects) go under the text instead of beside it. */
  stacked?: boolean;
}

/** Label and description on the left, the control on the right (or below when stacked). */
export function SettingRow({ icon, label, description, control, action, stacked }: SettingRowProps) {
  return <div className={'pa-set-row' + (stacked ? ' pa-set-row--stacked' : '')}>
    {icon && <span className="pa-set-icon" aria-hidden="true">{icon}</span>}
    <div className="pa-set-text"><strong>{label}</strong>{description && <span>{description}</span>}</div>
    {action && <div className="pa-set-action">{action}</div>}
    {control && <div className="pa-set-control">{control}</div>}
  </div>;
}
