import { createRoot } from 'react-dom/client';
import { ConfigProvider, theme } from 'antd';
import { PersonalAgentPanel } from '../src/components/personal-agent/PersonalAgentPanel';
import { personalApi, type PersonalAgent, type PersonalMessage, type PersonalMonitor } from '../src/components/personal-agent/api';
import 'antd/dist/reset.css';
import '../src/index.css';
import '../src/styles';
import '../src/components/personal-agent/personal-agent.css';

const params = new URLSearchParams(location.search);
const dark = params.has('dark');
document.documentElement.dataset.theme = dark ? 'dark' : 'light';
const agent: PersonalAgent = { id: 'visual-test', chat_id: 'visual-test', status: 'active', computer_status: 'not_provisioned',
  generation: 1, config: { name: 'Hugo', concurrency: 3, space_enabled: true, email_enabled: false, skill_ids: [], mcp_ids: [], approval_mode: 'ask' } };
const sample: [PersonalMessage['role'], string][] = [
  ['assistant', '我已经整理好本次报告的资料。'],
  ['user', '请按这个方向继续，先把关键结论发给我。'],
  ['assistant', '可以，我会先整理关键结论，再给出具体的执行建议。\n\n**1. 资料与信息来源**\n\n- 保留原始资料中的链接和数字，便于你逐项核对。\n- 使用公开文档作为补充，例如 [项目文档](https://example.com/docs)。\n\n**2. 后续安排**\n\n先完成草稿，收到你的反馈后再修改。需要你确认的操作，会继续显示在对话下方。\n\n你可以继续补充要求，也可以随时查看右侧的任务进度。'],
  ['user', '好的，也请保留附件中的表格。'],
];
const rows: PersonalMessage[] = params.has('empty') ? [] : sample.map(([role, content], i) => ({
  id: 'visual-' + i, seq: i + 1, role, content, work_item_ids: [], task_links: [],
}));
let busy = params.has('typing');
const snapshot = (): PersonalMonitor => ({ agent, devices: [], tasks: [], schedules: [], outputs: { total: 0, images: 0, documents: 0, other: 0, has_more: false, items: [] },
  file_memory: { available: true, pending: 0, retrying: 0 },
  turns: busy ? [{ message_id: 'visual-3', status: 'running', work_item_ids: [] }] : [] });
personalApi.initialize = async () => agent;
personalApi.monitor = async () => snapshot();
personalApi.messages = async () => [...rows];
personalApi.channels = async () => [];
personalApi.settings = async patch => { Object.assign(agent.config, patch); return agent; };
personalApi.send = async content => {
  rows.push({ id: crypto.randomUUID(), seq: rows.length + 1, role: 'user', content, work_item_ids: [], task_links: [] });
  busy = true;
  setTimeout(() => {
    rows.push({ id: crypto.randomUUID(), seq: rows.length + 1, role: 'assistant', content: '收到。我会保留这些要求，继续整理报告。', work_item_ids: [], task_links: [] });
    busy = false;
  }, 1800);
  return { message_id: rows[rows.length - 1].id, status: 'pending', work_item_ids: [] };
};
const style = document.createElement('style');
style.textContent = 'html,body,#root{height:100%;overflow:hidden}body{margin:0}*{box-sizing:border-box}' +
  (params.has('conversation') ? '.pa-side,.pa-header{display:none!important}.pa-panel{--pa-scroll-extension:0px;padding:16px 40px 0;gap:0}.pa-messages-inner{max-width:1120px}' : '');
document.head.append(style);
createRoot(document.getElementById('root')!).render(
  <ConfigProvider theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm, token: { fontFamily: 'Arial, "Microsoft YaHei", sans-serif' } }}>
    <PersonalAgentPanel />
  </ConfigProvider>,
);
