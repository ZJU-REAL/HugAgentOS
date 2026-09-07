import type { DeviceCapabilityItem } from '../api';

export const DEVICE_SOURCE_LABEL: Record<string, string> = { cloud: '云端', local: '本机', builtin: '内置', plugin: '插件' };

export function deviceCapabilityState(item: DeviceCapabilityItem, candidates: DeviceCapabilityItem[] = []): { text: string; tone: 'ok' | 'wait' | 'off' | 'bad' } {
  if (item.resolution.outcome === 'conflict') return { text: '同名冲突', tone: 'bad' };
  if (item.source === 'cloud' && item.resolution.outcome === 'shadowed'
    && candidates.some((candidate) => candidate.derived_from === item.install_id && candidate.resolution.outcome === 'chosen')) {
    return { text: '已被本机副本取代', tone: 'off' };
  }
  if (item.resolution.outcome === 'shadowed') return { text: '被同名遮蔽', tone: 'off' };
  if (item.enabled === false || item.state === 'disabled') return { text: '已停用', tone: 'off' };
  switch (item.state) {
    case 'ready':
      if (item.readiness?.ready === false) return { text: '组件未就绪', tone: 'wait' };
      return item.usable ? { text: '已就绪', tone: 'ok' } : { text: '当前不可用', tone: 'bad' };
    case 'schema_empty': return { text: '暂无工具定义', tone: 'wait' };
    case 'pending': return { text: '待下载', tone: 'wait' };
    case 'preparing': return { text: '准备中', tone: 'wait' };
    case 'failed': return { text: '准备失败', tone: 'bad' };
    case 'files_missing': return { text: '文件缺失', tone: 'bad' };
    default: return { text: item.state, tone: 'off' };
  }
}
export function canPrepareDeviceCapability(item: DeviceCapabilityItem): boolean {
  return item.kind !== 'mcp' && item.source === 'cloud' && (['pending', 'failed', 'files_missing'].includes(item.state) || (item.state === 'ready' && item.readiness?.ready === false));
}
export function canRemoveDeviceFiles(item: DeviceCapabilityItem): boolean {
  return item.kind !== 'mcp' && item.source === 'cloud' && item.state === 'ready';
}

export function canCreateDeviceLocalCopy(item: DeviceCapabilityItem): boolean {
  return item.kind === 'skill' && item.source === 'cloud' && item.state === 'ready' && item.usable;
}
/** 恢复必须显式选回仍可用的云端原版，不能用清除偏好间接猜测赢家。 */
export function cloudRestoreTarget(item: DeviceCapabilityItem, items: DeviceCapabilityItem[]): DeviceCapabilityItem | null {
  if (item.kind !== 'skill' || item.source !== 'local' || !item.derived_from) return null;
  return items.find((candidate) => candidate.install_id === item.derived_from && candidate.source === 'cloud'
    && candidate.kind === 'skill' && candidate.state === 'ready' && candidate.usable) || null;
}

export function canToggleDeviceCapability(item: DeviceCapabilityItem): boolean {
  return item.registered === true && item.kind !== 'mcp' && item.source !== 'builtin';
}

const DEPENDENCY_REASON_LABEL: Record<string, string> = {
  platform_incompatible: '当前操作系统不兼容',
  runtime_dependency_missing: '缺少运行依赖',
  runtime_command_missing: '找不到本机命令',
};

export function deviceDependencyReason(reason: string, translate: (text: string) => string): string {
  const separator = reason.indexOf(':');
  const code = separator < 0 ? reason : reason.slice(0, separator);
  const label = DEPENDENCY_REASON_LABEL[code];
  return label ? translate(label) + (separator < 0 ? '' : ': ' + reason.slice(separator + 1).trim()) : reason;
}
