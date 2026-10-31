import { useState, type ReactNode } from 'react';
import { CaretDownFilled } from '@ant-design/icons';

interface MonitorCardProps {
  title: string;
  /** Right side of the header: a count, a percentage, a short state. */
  summary?: ReactNode;
  defaultOpen?: boolean;
  className?: string;
  children: ReactNode;
}

export function MonitorCard({ title, summary, defaultOpen = true, className, children }: MonitorCardProps) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className={`jx-runMonitor-card${className ? ' ' + className : ''}`} data-open={open || undefined}>
      <button type="button" className="jx-runMonitor-cardHead" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="jx-runMonitor-cardTitle">{title}</span>
        <CaretDownFilled className="jx-runMonitor-caret" aria-hidden />
        {summary != null && <span className="jx-runMonitor-summary">{summary}</span>}
      </button>
      {open && <div className="jx-runMonitor-cardBody">{children}</div>}
    </section>
  );
}
