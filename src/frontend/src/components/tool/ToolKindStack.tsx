import { useMemo } from 'react';
import type { ToolCall } from '../../types';
import type { ToolCapability } from '../../utils/toolCapability';
import { useToolCapabilityResolver } from '../../hooks/useToolCapability';
import { ToolCapabilityIcon } from './ToolCapabilityIcon';
import { ToolIcon } from './toolIcons';
import type { ToolIconKind } from '../../utils/toolIconKind';
import { toolIconKind } from '../../utils/toolActionLabel';
import { t } from '../../i18n';

/** More kinds than this collapse into a leading "+N" chip. */
const MAX_VISIBLE = 3;

interface ToolKindStackProps {
  tools: ToolCall[];
  /** Effective status per tool, aligned with `tools`. */
  statuses: string[];
  /** Live batch: new chips slide in; history renders stay still. */
  live: boolean;
}

interface Kind {
  key: string;
  icon: ToolIconKind;
  capability: ToolCapability | null;
  running: boolean;
}

/**
 * The kinds of action a batch used, one chip each, in first-use order.
 *
 * Several calls of the same kind share a chip, which lights up again when that
 * kind runs again — a batch of six file reads is one file icon, not six. The
 * most recent kinds stay visible, so the running one never hides behind "+N".
 * Skill, plugin and connector tools show their own logo.
 */
export function ToolKindStack({ tools, statuses, live }: ToolKindStackProps) {
  const resolveCapability = useToolCapabilityResolver();
  const kinds = useMemo(() => {
    const byKey = new Map<string, Kind>();
    tools.forEach((tool, index) => {
      const capability = resolveCapability(tool);
      const icon = toolIconKind(tool.name);
      const key = capability ? `cap:${capability.kind}:${capability.id}` : `icon:${icon}`;
      const running = statuses[index] === 'running';
      const previous = byKey.get(key);
      byKey.set(key, previous ? { ...previous, running: previous.running || running } : { key, icon, capability, running });
    });
    return [...byKey.values()];
  }, [tools, statuses, resolveCapability]);

  if (!kinds.length) return null;
  const hidden = Math.max(0, kinds.length - MAX_VISIBLE);
  return (
    <span className="jx-kindStack" role="img" aria-label={t('{n} 个工具', { n: tools.length })}>
      {hidden > 0 && <span className="jx-kindChip jx-kindChip--more">+{hidden}</span>}
      {kinds.slice(hidden).map((kind) => (
        <span
          key={kind.key}
          className={`jx-kindChip${kind.running ? ' is-running' : ''}${live ? ' jx-kindChip--enter' : ''}${kind.capability ? ' jx-kindChip--logo' : ''}`}
        >
          {kind.capability
            ? <ToolCapabilityIcon capability={kind.capability} size={18} />
            : <ToolIcon kind={kind.icon} />}
        </span>
      ))}
    </span>
  );
}
