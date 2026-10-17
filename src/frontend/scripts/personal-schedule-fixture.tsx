import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ConfigProvider, Button } from 'antd';
import { PersonalSchedules } from '../src/components/personal-agent/scheduling/PersonalSchedules';
import { type PersonalSchedule } from '../src/components/personal-agent/api';
import '../src/components/personal-agent/personal-agent.css';
const initial: PersonalSchedule = { id: 'schedule-1', title: '邮件摘要', instruction: '阅读邮件', cron: '0 8 * * *', timezone: 'Asia/Shanghai', next_run: '2099-01-01T08:00:00+08:00' };
export function Fixture() {
  const [items, setItems] = useState([initial]);
  const [error, setError] = useState('');
  const refresh = async () => { const response = await fetch('/fixture-state'); setItems(await response.json()); };
  return <ConfigProvider theme={{ token: { motion: false } }}><div style={{ width: 360, padding: 20 }}>
    <h3>定时任务</h3><Button onClick={() => setItems([{ ...initial, cron: null, run_at: '2099-01-01T08:00:31+08:00', next_run: '2099-01-01T08:00:31+08:00' }])}>单次任务</Button><Button onClick={() => setItems([])}>清空列表</Button><div role="status">{error}</div>
    <PersonalSchedules items={items} onDone={refresh} onError={e => setError(String(e))} />
  </div></ConfigProvider>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
