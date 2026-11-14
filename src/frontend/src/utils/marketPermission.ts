import type { AuthUser } from '../api';

/** Resource field → the permission to install that kind from its market (mirrors backend MARKET_INSTALL_FLAG). */
export const MARKET_INSTALL_FLAG = {
  skill_ids: 'can_install_market_skill',
  mcp_ids: 'can_install_market_mcp',
  plugin_ids: 'can_install_market_plugin',
  agent_ids: 'can_install_market_agent',
} as const;

export type MarketField = keyof typeof MARKET_INSTALL_FLAG;

export const canInstallFromMarket = (user: AuthUser | null | undefined, field: MarketField): boolean =>
  user?.[MARKET_INSTALL_FLAG[field]] === true;
