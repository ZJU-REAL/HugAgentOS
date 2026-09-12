import type { ToolCapability } from '../../utils/toolCapability';
import { SkillAvatar } from '../catalog/skillIcons';
import { PluginAvatar } from '../catalog/PluginIconPicker';
import { McpIcon } from '../catalog/McpIcon';

export function ToolCapabilityIcon({ capability }: { capability: ToolCapability }) {
  return <span className="jx-tcr-capabilityIcon" title={capability.name}>
    {capability.kind === 'skill'
      ? <SkillAvatar icon={capability.icon || undefined} seed={capability.id} name={capability.name} size={22} round />
      : capability.kind === 'plugin'
        ? <PluginAvatar icon={capability.icon} size={22} round />
        : <McpIcon id={capability.id} icon={capability.icon || undefined} size={22} />}
  </span>;
}
