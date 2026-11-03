import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Button, ConfigProvider } from 'antd';
import { AttentionBar } from '../src/components/personal-agent/tasks/AttentionBar';
import { PersonalSearch } from '../src/components/personal-agent/conversation/PersonalSearch';
import { type PersonalAttention, type PersonalTask } from '../src/components/personal-agent/api';

export function Fixture() {
  const [now] = useState(() => Date.now());
  const [opened, setOpened] = useState('');
  const [refreshed, setRefreshed] = useState(0);
  const [errors, setErrors] = useState<string[]>([]);
  const [searching, setSearching] = useState(false);
  const tasks: PersonalTask[] = [
    { id: 'failed', goal: '整理周报', status: 'failed', waiting_reason: '任务执行失败。', updated_at: new Date(now).toISOString() },
    { id: 'blocked', goal: '订会议室', status: 'waiting', waiting_reason: '订 3 楼还是 5 楼？', question: '订 3 楼还是 5 楼？', updated_at: new Date(now).toISOString() },
  ];
  const attention: PersonalAttention = {
    turns: [{ message_id: 'pa_msg_turn', chat_id: 'main', preview: '帮我总结今天的会议', kind: 'message', at: new Date(now).toISOString() }],
    deliveries: [{ message_id: 'pa_msg_reply', chat_id: 'pa_chat_channel_x', preview: '会议总结如下', channel_type: 'lark', at: new Date(now).toISOString() }],
  };
  const done = async () => { setRefreshed(value => value + 1); };
  return <ConfigProvider theme={{ token: { motion: false } }}>
    <output data-testid="refreshed">{refreshed}</output>
    <output data-testid="opened">{opened}</output>
    <output data-testid="errors">{errors.join('|')}</output>
    <AttentionBar tasks={tasks} attention={attention} now={now} onDone={done} onError={exc => setErrors(list => [...list, String(exc)])} onOpen={task => setOpened('task:' + task.id)} />
    <Button onClick={() => setSearching(true)}>打开查找</Button>
    <PersonalSearch open={searching} mainChatId="main" onClose={() => setSearching(false)} onOpenTask={task => setOpened('task:' + task.id)} onSelectChat={id => setOpened('chat:' + (id || 'main'))} />
  </ConfigProvider>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
