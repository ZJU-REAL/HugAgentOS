import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Input, List, Modal, Space, Spin } from 'antd';
import { personalApi, errorText } from '../api';
import { subscribePersonalChanges, waitForTopic } from '../shared/liveFeed';
import { t } from '../../../i18n';
interface Chat { chat_id: string; title: string }
interface Message { message_id: string; role: string; content: string; chat_seq: number }
interface Receipt { id?: string; status: string; result: unknown; deadline?: number }

export function DeviceSessions({ target, name, onClose }: { target: string; name: string; onClose: () => void }) {
  const [chats, setChats] = useState<Chat[]>([]);const [page, setPage] = useState(1);const [more, setMore] = useState(false);const [chat, setChat] = useState<Chat | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);const [text, setText] = useState('');
  const [error, setError] = useState('');const [busy, setBusy] = useState(false);const [run, setRun] = useState<{ id: string; chatId: string } | null>(null);
  const alive = useRef(true);const refreshing = useRef(new Set<string>());const selectedId = useRef<string | null>(null);
  selectedId.current = chat?.chat_id || null;
  const operation = useCallback(async (operation: string, args: Record<string, unknown> = {}) => {
    let receipt = await personalApi.sessionOperation<Receipt>({ target, operation, args, request_key: crypto.randomUUID() });
    const end = Date.now() + 35000;
    const waiting = () => ['queued', 'sent'].includes(receipt.status) && alive.current && Date.now() < end;
    // The server signals when the computer's receipt lands. Listening starts before each read, so a receipt that
    // lands in between is not missed.
    while (waiting()) {
      const stop = new AbortController();
      const signalled = waitForTopic('operation:' + receipt.id, end - Date.now(), stop.signal);
      receipt = await personalApi.sessionOperation<Receipt>(undefined, receipt.id);
      if (waiting()) await signalled;
      stop.abort();
    }
    if (receipt.status !== 'completed') throw new Error(t('操作尚未确认。请检查会话后再重试。') + ' ' + (receipt.id || ''));
    if (receipt.result && typeof receipt.result === 'object' && ('error' in receipt.result || 'outcome_unknown' in receipt.result)) throw new Error(t('操作尚未确认。请检查会话后再重试。'));
    return receipt.result;
  }, [target]);
  const act = useCallback(async (action: () => Promise<void>) => {
    setBusy(true);setError('');
    try { await action(); } catch (exc) { if (alive.current) setError(errorText(exc)); }
    finally { if (alive.current) setBusy(false); }
  }, []);
  const load = useCallback(async (requestedPage = 1) => {
    const result = await operation('chat.list', { page: requestedPage, page_size: 100 }) as { items: Chat[]; pagination: { has_next: boolean } };
    if (alive.current) { setChats(result.items);setPage(requestedPage);setMore(result.pagination.has_next); }
  }, [operation]);
  const again = useRef(new Set<string>());
  const refresh = useCallback(async (id: string): Promise<void> => {
    // A change signalled during a read is read again once that read ends, not dropped.
    if (refreshing.current.has(id)) { again.current.add(id); return; }
    refreshing.current.add(id);
    try {
      const history = await operation('chat.history', { chat_id: id, page_size: 100, order: 'desc' }) as { items: Message[] };
      const active = await operation('run.active', { chat_id: id }) as { run_id?: string } | null;
      if (alive.current && selectedId.current === id) { setMessages([...history.items].sort((a, b) => a.chat_seq - b.chat_seq));setRun(active?.run_id ? { id: active.run_id, chatId: id } : null); }
    } finally {
      refreshing.current.delete(id);
      if (again.current.delete(id) && alive.current && selectedId.current === id) await refresh(id);
    }
  }, [operation]);
  useEffect(() => { alive.current = true;void act(() => load());return () => { alive.current = false; }; }, [act, load]);
  const chatId = chat?.chat_id;
  useEffect(() => {
    if (!chatId) return;
    void act(() => refresh(chatId));
  }, [chatId, act, refresh]);
  // The computer reports its own chat changes; only what changed is read again, and everything after a reconnect.
  const pageRef = useRef(page);
  pageRef.current = page;
  useEffect(() => subscribePersonalChanges(signal => {
    const failed = (exc: unknown) => { if (alive.current) setError(errorText(exc)); };
    const selected = selectedId.current;
    if (signal.type === 'down') return;
    const all = signal.type === 'ready';
    if (all || signal.topics.includes(target)) void load(pageRef.current).catch(failed);
    if (selected && (all || signal.topics.includes(target + ':' + selected))) void refresh(selected).catch(failed);
  }), [target, load, refresh]);
  return <Modal open title={name + ' · ' + t('会话管理')} onCancel={onClose} footer={null} width={840}>
    {error && <Alert type="error" message={error} />}
    <Space><Button disabled={busy} onClick={() => void act(async () => { const created = await operation('chat.create', { title: t('新对话') }) as Chat;await load();setChat(created); })}>{t('新建对话')}</Button>
      <Button disabled={busy} onClick={() => void act(() => load(page))}>{t('刷新')}</Button><Button disabled={busy || page === 1} onClick={() => void act(() => load(page - 1))}>{t('上一页')}</Button><Button disabled={busy || !more} onClick={() => void act(() => load(page + 1))}>{t('下一页')}</Button></Space>
    <Spin spinning={busy}><div className="pa-device-session-grid">
      <List dataSource={chats} renderItem={item => <List.Item><Button type={chat?.chat_id === item.chat_id ? 'primary' : 'text'} onClick={() => { setMessages([]);setRun(null);setChat(item); }}>{item.title}</Button></List.Item>} />
      <div>{chat && <>
        <Input aria-label={t('会话名称')} value={chat.title} onChange={event => setChat({ ...chat, title: event.target.value })} />
        <Space><Button disabled={busy} onClick={() => void act(async () => { await operation('chat.update', { chat_id: chat.chat_id, title: chat.title });await load(); })}>{t('保存')}</Button>
          <Button danger disabled={busy} onClick={() => Modal.confirm({ title: t('删除会话'), content: chat.title, onOk: () => act(async () => { await operation('chat.delete', { chat_id: chat.chat_id });setChat(null);setMessages([]);await load(); }) })}>{t('删除')}</Button></Space>
        <div style={{ maxHeight: 420, overflowY: 'auto' }}>{messages.map(message => <div key={message.message_id}><strong>{message.role === 'user' ? t('你') : t('助手')}</strong><p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{message.content}</p></div>)}</div>
        <Input.TextArea aria-label={t('发送消息')} value={text} onChange={event => setText(event.target.value)} />
        <Space><Button disabled={busy || !text.trim()} onClick={() => void act(async () => {
          if (run?.chatId === chat.chat_id) await operation('run.steer', { run_id: run.id, message: text, steer_id: crypto.randomUUID(), delivery_mode: 'followUp' });
          else { const started = await operation('chat.send', { chat_id: chat.chat_id, message: text }) as { run_id: string };if (selectedId.current === chat.chat_id) setRun({ id: started.run_id, chatId: chat.chat_id }); }
          setText('');await refresh(chat.chat_id);
        })}>{run ? t('追加任务') : t('发送')}</Button>
          {run && <Button danger disabled={busy} onClick={() => void act(async () => { await operation('run.cancel', { run_id: run.id });await refresh(chat.chat_id); })}>{t('停止')}</Button>}</Space>
      </>}</div>
    </div></Spin>
  </Modal>;
}
