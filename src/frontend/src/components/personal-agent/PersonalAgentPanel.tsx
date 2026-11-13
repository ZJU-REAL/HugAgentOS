import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Input } from 'antd';
import { ArrowUpOutlined, CloseOutlined, HeartOutlined, PlusOutlined, SearchOutlined, UnorderedListOutlined } from '@ant-design/icons';
import { uploadFile } from '../../api';
import { PersonalSettings } from './settings/PersonalSettings';
import { AttentionBar } from './tasks/AttentionBar';
import { PersonalConfirmBar } from './tasks/PersonalConfirmBar';
import { PersonalApprovalPill } from './tasks/PersonalApprovalPill';
import { PersonalMessageRow } from './conversation/PersonalMessageRow';
import { workSummary } from './tasks/taskState';
import { PersonalAvatar } from './shared/PersonalAvatar';
import { PersonalSide } from './PersonalSide';
import { ProfileModal } from './settings/ProfileModal';
import { ChannelMark } from './channels/ChannelEntry';
import { CHANNELS } from './channels/channelMeta';
import { DEFAULT_AGENT_NAME } from './shared/avatarPresets';
import { useAgentNameStore } from './shared/agentName';
import { useConversationFeed } from './conversation/useConversationFeed';
import { useConversationScroll } from './conversation/useConversationScroll';
import { usePersonalResultRead } from './conversation/usePersonalResultRead';
import { t } from '../../i18n';
import { personalApi, errorText, type PersonalMessage, type PersonalTask } from './api';
import { PersonalTaskDialog } from './tasks/PersonalTaskDialog';
import { PersonalSearch } from './conversation/PersonalSearch';
import './personal-agent.css';

interface ConversationDraft { text: string; attachments: { file_id: string; name: string }[]; requestKey?: string }

export function PersonalAgentPanel() {
  const [chatId, setChatId] = useState<string>();
  const [drafts, setDrafts] = useState<Record<string, ConversationDraft>>({});
  const saveDraft = useCallback((id: string, update: (draft: ConversationDraft) => ConversationDraft) => setDrafts(previous => ({ ...previous, [id]: update(previous[id] || { text: '', attachments: [] }) })), []);
  return <ConversationPanel key={chatId || 'main'} chatId={chatId} onSelectChat={setChatId} savedDraft={drafts[chatId || 'main']} saveDraft={saveDraft} />;
}

function ConversationPanel({ chatId, onSelectChat, savedDraft, saveDraft }: { chatId?: string; onSelectChat: (id?: string) => void; savedDraft?: ConversationDraft; saveDraft: (id: string, update: (draft: ConversationDraft) => ConversationDraft) => void }) {
  // Draft ownership survives keyed conversation unmounts and late send responses.
  const draft = savedDraft?.text || '';
  const attachments = savedDraft?.attachments || [];
  const setDraft = (text: string) => saveDraft(chatId || 'main', previous => ({ ...previous, text }));
  const setAttachments = (update: (files: ConversationDraft['attachments']) => ConversationDraft['attachments']) =>
    saveDraft(chatId || 'main', previous => ({ ...previous, attachments: update(previous.attachments) }));
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const [error, setError] = useState('');
  const fail = (exc: unknown) => setError(errorText(exc));
  const { conversations, monitor, messages, older, loadingOlder, pollError, checkedAt, loading, refreshRef, history } = useConversationFeed(chatId, setError);
  const [sending, setSending] = useState(false);
  const [selectedTask, setSelectedTask] = useState<PersonalTask>();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [sideOpen, setSideOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const requestKey = savedDraft?.requestKey;
  const setRequestKey = (key: string | undefined) => saveDraft(chatId || 'main', previous => ({ ...previous, requestKey: key }));
  // Like a chat app, a reply shows only once it is complete; until then a typing line sits under the last message.
  // A heartbeat check usually ends quiet: it shows no typing line unless it writes an alert.
  const visible = messages.filter(row => !row.streaming);
  const typing = !!monitor?.turns?.some(turn => !turn.heartbeat) || messages.some(row => row.streaming && row.content.trim());
  // What was last sent shows at once and stays until the stored copy (or a reply after it) arrives: on the desktop
  // app that can be seconds after the send request returns. The two are never shown together.
  const [sent, setSent] = useState<{ key: string; text: string; afterSeq: number }>();
  const stored = sent && visible.find(row => row.role === 'user' && row.seq > sent.afterSeq && row.content.trim() === sent.text);
  const answered = sent && visible.some(row => row.role !== 'user' && row.seq > sent.afterSeq);
  const pendingRow: PersonalMessage | undefined = sent && !stored && !answered
    ? { id: 'pending:' + sent.key, seq: Number.MAX_SAFE_INTEGER, role: 'user', content: sent.text, work_item_ids: [], task_links: [] }
    : undefined;
  const scroll = useConversationScroll({ firstId: visible[0]?.id, hasOlder: older, loadingOlder, loadOlder: history });
  usePersonalResultRead(scroll.viewport, chatId ? [] : messages);
  const agent = monitor?.agent;
  const name = agent?.config.name || DEFAULT_AGENT_NAME;
  // Navigation lists outside the panel follow a rename made here or by the agent.
  const setSharedName = useAgentNameStore(s => s.setName);
  useEffect(() => { if (agent) setSharedName(agent.config.name); }, [agent, agent?.config.name, setSharedName]);

  async function send() {
    if ((!draft.trim() && !attachments.length) || sending || uploading) return;
    const key = requestKey || crypto.randomUUID();
    const content = draft.trim() || t('请读取附件，并根据内容给出回复。');
    setRequestKey(key);
    setSending(true);
    // Shown now, not when the request returns; the composer keeps the draft until it succeeds.
    setSent({ key, text: content, afterSeq: messages.reduce((max, row) => Math.max(max, row.seq), 0) });
    scroll.stickToBottom();
    try {
      // The agent answers directly, or starts or changes background tasks itself.
      await personalApi.send(content, key, [], attachments.map(file => file.file_id), 'auto', undefined, chatId);
      saveDraft(chatId || 'main', previous => previous.requestKey === key
        ? { text: '', attachments: [] } : previous);
      setError('');
      await refreshRef.current();
    } catch (exc) {
      // The draft is back in the composer, so the sent copy goes.
      setSent(previous => previous?.key === key ? undefined : previous);
      fail(exc);
    }
    finally { setSending(false); }
  }
  async function attach(files: FileList | null) {
    if (!files || !agent) return;
    if (attachments.length + files.length > 10) { setError(t('最多添加十个附件')); return; }
    setUploading(true);
    try {
      const uploaded = await Promise.all(Array.from(files).map(file => uploadFile(file, chatId || agent.chat_id, undefined, { target: 'cloud', uploadKey: crypto.randomUUID() })));
      setAttachments(previous => [...previous, ...uploaded]); setRequestKey(undefined);
    } catch (exc) { fail(exc); }
    finally { setUploading(false); if (fileInput.current) fileInput.current.value = ''; }
  }
  function author(row: PersonalMessage) {
    if (row.role === 'assistant') return row.heartbeat ? <span className="pa-message-author"><HeartOutlined aria-hidden="true" />{t('主动服务')}</span> : null;
    if (row.origin === 'schedule') return <span className="pa-message-author">{t('定时任务')}</span>;
    const channel = CHANNELS.find(item => item.type === row.channel_type);
    // Your own messages sit on the right without a name; only an outside channel is labelled.
    return channel ? <span className="pa-message-author"><span className="pa-message-source"><ChannelMark type={channel.type} size={16} />{t('来自{channel}', { channel: t(channel.label) })}</span></span> : null;
  }

  const tasks = monitor?.tasks || [];
  const status = workSummary(tasks, monitor?.attention, checkedAt);
  const typingLine = <span className="pa-typing" role="status" aria-label={t('{name} 正在回复', { name })}>
    <span className="pa-typing-dots" aria-hidden="true"><i /><i /><i /></span>
  </span>;

  return <section className="pa-panel" aria-label="Personal Agent">
    <header className="pa-header">
      <PersonalAvatar avatar={agent?.config.avatar} name={name} size={30} />
      <div className="pa-header-identity"><strong>{name}{chatId && <span className="pa-message-source"> · {t('渠道会话')}</span>}</strong>{status && <span className="pa-header-status" title={status}>{status}</span>}</div>
      <Button className="pa-header-action" type="text" icon={<SearchOutlined />} aria-label={t('查找消息或任务')} title={t('查找消息或任务')} onClick={() => setSearchOpen(true)} />
      <Button className="pa-side-toggle" type="text" icon={<UnorderedListOutlined />} aria-label={t('打开状态栏')} onClick={() => setSideOpen(value => !value)} />
    </header>
    {error && <Alert type="error" message={error} closable onClose={() => setError('')} />}
    {pollError && <Alert type="warning" message={pollError} />}
    {/* The composer docks inside the scroller so its scrollbar runs down to the bottom of the panel. */}
    <div ref={scroll.setViewport} className="pa-messages" onScroll={scroll.onScroll}>
      <div ref={scroll.setContent} className="pa-messages-inner" role="log" aria-live="polite" aria-busy={loading}>
      {loading && <div className="jx-skeletonBlock pa-skeleton" aria-label={t('加载中')} />}
      {!loading && !messages.length && <div className="pa-empty"><PersonalAvatar avatar={agent?.config.avatar} name={name} size={72} />
        <h2>{t('你好，我是 {name}', { name })}</h2><p>{t('把事情交给我。我会在后台处理，关掉页面也不会停。')}</p></div>}
      {!loading && older && <div ref={scroll.setTopSentinel} className="pa-older" aria-hidden={!loadingOlder}>
        {loadingOlder && <span role="status">{t('正在加载更早的消息…')}</span>}
      </div>}
      {visible.map(row => <PersonalMessageRow key={row.id} row={row} author={author(row)} />)}
      {pendingRow && <PersonalMessageRow key={pendingRow.id} row={pendingRow} author={null} pending />}
      {typing && <div className="pa-typing-row" data-testid="pa-typing">{typingLine}</div>}
      </div>
      <div ref={scroll.setDock} className="pa-dock">
        <PersonalConfirmBar tasks={tasks} onDone={refreshRef.current} onError={fail} />
        <AttentionBar tasks={tasks} attention={monitor?.attention} chatId={chatId} now={checkedAt} onDone={refreshRef.current} onError={fail} onOpen={setSelectedTask} />
        <div className="pa-composer">
          <input type="file" multiple hidden ref={fileInput} aria-label={t('添加附件')} onChange={event => void attach(event.target.files)} />
          {!!attachments.length && <div className="pa-composer-files">{attachments.map(file => <Button key={file.file_id} size="small" onClick={() => { setAttachments(previous => previous.filter(item => item.file_id !== file.file_id)); setRequestKey(undefined); }}>{file.name} <CloseOutlined /></Button>)}</div>}
          <div className="pa-composer-input">
            <Button className="pa-composer-attach" type="text" shape="circle" icon={<PlusOutlined />} aria-label={t('添加附件')} loading={uploading} disabled={sending} onClick={() => fileInput.current?.click()} />
            <Input.TextArea aria-label={t('发送给 Personal Agent')} placeholder={t('给 {name} 发消息，交代一件事', { name })} value={sending ? '' : draft} disabled={sending} autoSize={{ minRows: 1, maxRows: 7 }} onChange={event => { setDraft(event.target.value); setRequestKey(undefined); }} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void send(); } }} />
            <Button className="pa-composer-send" aria-label={t('发送')} type="primary" shape="circle" icon={<ArrowUpOutlined />} loading={sending} disabled={(!draft.trim() && !attachments.length) || uploading} onClick={() => void send()} />
          </div>
        </div>
        <div className="pa-composer-preferences"><PersonalApprovalPill mode={agent?.config.approval_mode} onChange={refreshRef.current} /></div>
      </div>
    </div>
    <PersonalSide monitor={monitor} name={name} open={sideOpen} onClose={() => setSideOpen(false)} onProfile={() => setProfileOpen(true)}
      conversations={conversations} selectedChatId={chatId} onSelectChat={onSelectChat}
      onSettings={() => setSettingsOpen(true)} onOpenTask={setSelectedTask} onRefresh={refreshRef.current} onError={fail} now={checkedAt} />
    {selectedTask && <PersonalTaskDialog key={selectedTask.id} task={monitor?.tasks.find(task => task.id === selectedTask.id) || selectedTask} onClose={() => setSelectedTask(undefined)} onChange={refreshRef.current} />}
    <PersonalSearch open={searchOpen} mainChatId={agent?.chat_id} onClose={() => setSearchOpen(false)} onOpenTask={setSelectedTask} onSelectChat={onSelectChat} />
    {agent && profileOpen && <ProfileModal agent={agent} open onClose={() => setProfileOpen(false)} onSaved={refreshRef.current} />}
    <PersonalSettings open={settingsOpen} monitor={monitor} name={name} onClose={() => setSettingsOpen(false)} onProfile={() => { setSettingsOpen(false); setProfileOpen(true); }} onChange={refreshRef.current} />
  </section>;
}
