interface InvocationBadgesProps {
  mentionName?: string;
  skillName?: string;
  pluginName?: string;
  connectorName?: string;
  className?: string;
}

/** Shared display contract for explicit agent/skill/plugin/connector invocation. */
export function InvocationBadges({
  mentionName,
  skillName,
  pluginName,
  connectorName,
  className,
}: InvocationBadgesProps) {
  const badges = [
    mentionName ? { prefix: '@', name: mentionName, kind: 'mention' } : null,
    skillName ? { prefix: '/', name: skillName, kind: 'skill' } : null,
    pluginName ? { prefix: '/', name: pluginName, kind: 'plugin' } : null,
    connectorName ? { prefix: 'MCP', name: connectorName, kind: 'connector' } : null,
  ].filter((item): item is { prefix: string; name: string; kind: string } => item !== null);

  if (badges.length === 0) return null;
  return (
    <div className={['jx-msgChipBadges', className].filter(Boolean).join(' ')}>
      {badges.map((item) => (
        <span key={item.kind} className={`jx-msgChip jx-msgChip--${item.kind}`}>
          <span className="jx-msgChip-prefix">{item.prefix}</span>{item.name}
        </span>
      ))}
    </div>
  );
}
