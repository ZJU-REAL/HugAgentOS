import { useDelayedFlag } from '../../hooks/useDelayedFlag';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import './capabilitySync.css';

/** 工作台里与云端的连接中断时的常驻提示：壳在后台自动重连，用户不需要做任何事。 */
export function CloudReconnectNotice() {
  const reconnecting = useDeploymentModeStore((s) => s.capabilitySyncRetrying && !!s.capabilitySyncError);
  // 一次续期抖动几秒内就恢复，不值得打断用户。
  const visible = useDelayedFlag(reconnecting, { showAfter: 3000, minVisible: 1000 });
  if (!visible) return null;
  return <div className="jx-cloudReconnect" role="status">与云端的连接已中断，正在自动重连…</div>;
}
