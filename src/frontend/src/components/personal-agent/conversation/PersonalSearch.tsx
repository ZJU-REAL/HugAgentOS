import { useRef, useState } from 'react';
import { Button, Empty, Input, Modal } from 'antd';
import { personalApi, errorText, type PersonalSearchResult, type PersonalTask } from '../api';
import { taskTitle } from '../tasks/taskState';
import { t } from '../../../i18n';

const STATUS: Record<string, string> = { queued: '排队中', running: '执行中', waiting: '等待处理', completed: '已完成', failed: '失败', cancelled: '已取消' };

interface PersonalSearchProps {
  open: boolean;
  mainChatId?: string;
  onClose: () => void;
  onOpenTask: (task: PersonalTask) => void;
  onSelectChat: (chatId?: string) => void;
}

/** Find where an IM message went: paste its link or message ID (or some of its text) and open its task or conversation. */
export function PersonalSearch({ open, mainChatId, onClose, onOpenTask, onSelectChat }: PersonalSearchProps) {
  const [query, setQuery] = useState('');
  const [result, setResult] = useState<PersonalSearchResult>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const current = useRef<AbortController | undefined>(undefined);

  async function search(value: string) {
    const text = value.trim();
    if (!text) return;
    current.current?.abort();
    const controller = new AbortController();
    current.current = controller;
    setLoading(true); setError('');
    try { setResult(await personalApi.search(text, controller.signal)); }
    catch (exc) { if (!controller.signal.aborted) setError(errorText(exc)); }
    finally { if (current.current === controller) setLoading(false); }
  }
  const tasks = result?.tasks || [];
  const messages = result?.messages || [];
  return <Modal open={open} title={t('查找消息或任务')} footer={null} onCancel={onClose} destroyOnHidden>
    <Input.Search aria-label={t('消息链接、消息 ID 或内容')} placeholder={t('例如 om_xxx、Slack 消息链接，或消息里的一句话')}
      value={query} onChange={event => setQuery(event.target.value)} onSearch={value => void search(value)} loading={loading} enterButton={t('查找')} allowClear />
    {error && <p className="pa-search-error" role="alert">{error}</p>}
    {result && !tasks.length && !messages.length && <Empty className="pa-search-empty" image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('没有找到。飞书的分享链接不含消息 ID，可以粘贴消息里的一句话。')} />}
    {!!tasks.length && <section className="pa-search-group" aria-label={t('任务')}>
      <h4>{t('任务')}</h4>
      {tasks.map(task => <Button key={task.id} type="text" className="pa-search-row" onClick={() => { onOpenTask(task); onClose(); }}>
        <span className="pa-search-title">{taskTitle(task).slice(0, 80)}</span>
        <small>{task.outcome === 'dropped' ? t('无需处理') : t(STATUS[task.status] || task.status)}</small>
      </Button>)}
    </section>}
    {!!messages.length && <section className="pa-search-group" aria-label={t('消息')}>
      <h4>{t('消息')}</h4>
      {messages.map(item => <Button key={item.message_id} type="text" className="pa-search-row" onClick={() => { onSelectChat(item.chat_id === mainChatId ? undefined : item.chat_id); onClose(); }}>
        <span className="pa-search-title">{item.preview || t('（无文字）')}</span>
        <small>{item.role === 'user' ? t('你发的消息') : t('回复')} · {new Date(item.at).toLocaleString()}</small>
      </Button>)}
    </section>}
  </Modal>;
}
