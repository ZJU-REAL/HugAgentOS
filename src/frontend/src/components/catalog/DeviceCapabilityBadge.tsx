import { useEffect } from 'react';
import type { DeviceCapabilityKind } from '../../api';
import { t } from '../../i18n';
import { useDesktopCapabilityStore } from '../../stores/desktopCapabilityStore';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { DEVICE_SOURCE_LABEL } from '../../utils/deviceCapabilities';

/** 双模式下在能力卡片上标注这条能力来自本机还是云端。纯展示，没有任何操作。 */
export function DeviceCapabilityBadge({ kind, runtimeName }: {
  kind: DeviceCapabilityKind; runtimeName: string;
}) {
  const enabled = useDeploymentModeStore((s) => s.provisionMode === 'dual');
  const load = useDesktopCapabilityStore((s) => s.load);
  const state = useDesktopCapabilityStore((s) => s.kinds[kind]);
  useEffect(() => { if (enabled) void load(kind); }, [enabled, kind, load]);
  if (!enabled) return null;
  const item = state.byName[runtimeName]
    || state.items.find((candidate) => candidate.server_id === runtimeName);
  if (!item) return null;
  return (
    <span className={`jx-devcap-chip jx-devcap-src-${item.source}`}>
      {t(DEVICE_SOURCE_LABEL[item.source] || item.source)}
    </span>
  );
}
