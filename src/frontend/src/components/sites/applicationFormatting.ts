import { stablePublicOrigin } from '../../stores/deploymentModeStore';

export function applicationMcpUrl(appId: string): string {
  return `${stablePublicOrigin()}/applications-mcp/${appId}`;
}
