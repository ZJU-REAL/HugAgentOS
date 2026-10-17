import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ConfigProvider, Button } from 'antd';
import { AttentionBar } from '../src/components/personal-agent/tasks/AttentionBar';
import { TaskActions } from '../src/components/personal-agent/tasks/TaskActions';
import { type PersonalTask } from '../src/components/personal-agent/api';

export function Fixture() {
  const [now] = useState(() => Date.now());
  const [status, setStatus] = useState('running');
  const task: PersonalTask = { id: 'test', goal: '电脑操作', status, run_id: "test-run",
    uncertain_actions: status === 'completed' ? [] : ['action'], updated_at: new Date(now).toISOString() };
  const done = async () => {};
  return <ConfigProvider theme={{ token: { motion: false } }}>
    <Button onClick={() => setStatus('queued')}>模拟自动等待电脑</Button>
    <Button onClick={() => setStatus('waiting')}>模拟结果未知</Button>
    <Button onClick={() => setStatus('running')}>模拟执行中</Button>
    <Button onClick={() => setStatus('completed')}>模拟执行成功</Button>
    <AttentionBar tasks={[task]} now={now} onDone={done} onError={() => {}}
      onOpen={() => {}} />
    <div data-testid="task-dialog"><TaskActions task={task} onDone={done} onError={() => {}} /></div>
  </ConfigProvider>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
