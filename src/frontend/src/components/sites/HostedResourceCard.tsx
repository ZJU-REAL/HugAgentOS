import type { ReactNode } from 'react';

/** Sites and hosted MCP use the same visual and keyboard-accessible card shell. */
export function HostedResourceCard({ title, tags, address, meta, actions }: {
  title: string; tags: ReactNode; address?: ReactNode; meta: ReactNode; actions: ReactNode;
}) {
  return <div className="jx-sites-card jx-card-lift">
    <div className="jx-sites-cardMain">
      <div className="jx-sites-cardHead">
        <span className="jx-sites-cardTitle">{title}</span>
        {tags}
      </div>
      {address}
      <div className="jx-sites-cardMeta">{meta}</div>
    </div>
    <div className="jx-sites-cardActions">{actions}</div>
  </div>;
}
