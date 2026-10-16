/** Community edition: the Personal Agent module is not installed. */
export function usePersonalAgentAvailable(): boolean {
  return false;
}

export function usePersonalAgentName(): string {
  return '';
}

export function PersonalAgentPanel() {
  return null;
}

export function AgentWorkspaceView() {
  return null;
}

export function PersonalAgentSidebarEntry(_props: { onOpen: () => void }) {
  return null;
}

export function usePersonalAgentNotifications() {}
export function PersonalAgentNavStatus(_props: {compact?:boolean}) { return null; }

export function usePersonalAgentStatusText() { return ''; }
