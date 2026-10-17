import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ConfigProvider, Button } from 'antd';
import { PersonalCapabilities } from '../src/components/personal-agent/settings/PersonalCapabilities';
import { personalRequest, type PersonalAgent } from '../src/components/personal-agent/api';
import { useAuthStore } from '../src/stores';
import '../src/components/personal-agent/personal-agent.css';

useAuthStore.setState({ authUser: { can_add_skill: true, can_add_mcp: true, can_import_plugin: true } as NonNullable<ReturnType<typeof useAuthStore.getState>['authUser']> });
const initial: PersonalAgent = { id: 'pa-test', chat_id: 'pa-chat', status: 'ready',
  computer_status: 'ready', generation: 1, config: { concurrency: 3, space_enabled: true,
  email_enabled: true, capability_selection: { skill_ids: ['document', 'unavailable-skill', 'plugin-skill'], mcp_ids: ['search'], plugin_ids: ['sample'] }, skill_ids: ['document'], mcp_ids: ['search'],
  plugins: [{ id: 'sample', name: '示例插件', skill_ids: [], mcp_ids: [] }] } };
export function Fixture() {
  const [agent, setAgent] = useState(initial);
  const [open, setOpen] = useState(true);
  return <ConfigProvider theme={{ token: { motion: false } }}>
    <main style={{ maxWidth: 560, margin: '20px auto', padding: 16 }}>
      <Button onClick={() => setOpen(!open)}>切换设置</Button>
      {open && <PersonalCapabilities agent={agent} onChange={async () =>
        setAgent(await personalRequest<PersonalAgent>('/v1/personal-agent'))} />}
    </main>
  </ConfigProvider>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
