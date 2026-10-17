/* eslint-disable react-refresh/only-export-components -- Edition seam intentionally exports hooks and lazy components together. */
import { usePersonalAgentStatusText } from './components/personal-agent/shared/notifications';
import { PersonalAgentNavStatus } from './components/personal-agent/shared/PersonalAgentNavStatus';
export { PersonalAgentNavStatus, usePersonalAgentStatusText };
export { usePersonalAgentNotifications } from './components/personal-agent/shared/notifications';
/**
 * Personal Agent module seam. Shared components reach the module only through this
 * file in both CE and EE. The server reports availability in
 * `can_use_personal_agent`: true only when the deployment enables the module
 * (PERSONAL_AGENT_ENABLED) and the account policy allows access.
 */
import { RobotOutlined } from '@ant-design/icons';
import { lazy, useEffect } from 'react';
import { useAuthStore } from './stores';
import { DEFAULT_AGENT_NAME } from './components/personal-agent/shared/avatarPresets';
import { useAgentNameStore } from './components/personal-agent/shared/agentName';

export function usePersonalAgentAvailable(): boolean {
  return useAuthStore(s => s.authUser?.can_use_personal_agent === true);
}

/** The agent's current name for navigation labels; empty when the module is unavailable. */
export function usePersonalAgentName(): string {
  const available = usePersonalAgentAvailable();
  const userId = useAuthStore(s => s.authUser?.user_id);
  const owner = useAgentNameStore(s => s.owner);
  const name = useAgentNameStore(s => s.name);
  const load = useAgentNameStore(s => s.load);
  useEffect(() => { void load(); }, [available, userId, load]);
  return available ? (owner === userId ? name : DEFAULT_AGENT_NAME) : '';
}

export const PersonalAgentPanel = lazy(() => import('./components/personal-agent/PersonalAgentPanel').then(m => ({ default: m.PersonalAgentPanel })));
export const AgentWorkspaceView = lazy(() => import('./components/personal-agent/workspace/AgentWorkspaceView').then(m => ({ default: m.AgentWorkspaceView })));

export function PersonalAgentSidebarEntry({ onOpen }: { onOpen: () => void }) {
  const name = usePersonalAgentName();
  const statusText = usePersonalAgentStatusText();
  if (!name) return null;
  return <button className="jx-navItem" onClick={onOpen} title={name} aria-label={[name,statusText].filter(Boolean).join(' · ')}><RobotOutlined className="jx-navItemIcon" aria-hidden="true" /><span className="jx-personalNavName">{name}</span><PersonalAgentNavStatus /></button>;
}
