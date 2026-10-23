import type { ToolCapability } from '../../utils/toolCapability';
import { SkillAvatar } from '../catalog/skillIcons';
import { PluginAvatar } from '../catalog/PluginIconPicker';
import { McpIcon } from '../catalog/McpIcon';

export function ToolCapabilityIcon({ capability, size = 22 }: { capability: ToolCapability; size?: number }) {
  return <span className="jx-tcr-capabilityIcon" title={capability.name}>
    {capability.kind === 'skill'
      ? <SkillAvatar icon={capability.icon || undefined} seed={capability.id} name={capability.name} size={size} round />
      : capability.kind === 'plugin'
        ? <PluginAvatar icon={capability.icon} size={size} round />
        : <McpIcon id={capability.id} icon={capability.icon || undefined} size={size} />}
  </span>;
}
