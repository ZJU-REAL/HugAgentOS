import type { DeviceCapabilityItem } from '../api';
import type { SkillItem } from '../types';

export function mergeDeviceSkills(catalog: SkillItem[], device: DeviceCapabilityItem[]): SkillItem[] {
  const merged = [...catalog];
  const ids = new Set(catalog.map((item) => item.id));
  for (const item of device) {
    if (item.kind !== 'skill' || item.source !== 'local' || ids.has(item.runtime_name)) continue;
    ids.add(item.runtime_name);
    merged.push({
      id: item.runtime_name, name: item.display_name || item.runtime_name,
      desc: item.description || '', enabled: !!item.enabled, owner: 'device',
    });
  }
  return merged;
}
