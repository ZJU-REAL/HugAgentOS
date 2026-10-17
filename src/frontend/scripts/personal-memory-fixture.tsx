import React from 'react';
import { createRoot } from 'react-dom/client';
import { PersonalSettings } from '../src/components/personal-agent/settings/PersonalSettings';
import { useModelCapabilitiesStore } from '../src/stores';
import type { PersonalMonitor } from '../src/components/personal-agent/api';
import { bindRouter } from '../src/routing/navigation';
import '../src/styles/variables.css';
import '../src/components/personal-agent/personal-agent.css';
bindRouter({ navigate: to => window.history.pushState({}, '', to) });
useModelCapabilitiesStore.setState({ loaded: true });
const monitor = { agent: { id: 'agent', chat_id: 'chat', status: 'ready', computer_status: 'ready', generation: 1,
  config: { name: 'Hugo', timezone: 'Asia/Shanghai', capability_selection: { skill_ids: [], mcp_ids: [], plugin_ids: [] } } },
  tasks: [], turns: [], devices: [], outputs: { total: 0, images: 0, documents: 0, other: 0, has_more: false, items: [] },
  file_memory: { available: true, pending: 2, retrying: 1 } } as PersonalMonitor;
createRoot(document.getElementById('root')!).render(<PersonalSettings open name="Hugo" monitor={monitor}
  onClose={() => { document.body.dataset.closed = 'true'; }} onProfile={() => {}} onChange={async () => {}} />);
