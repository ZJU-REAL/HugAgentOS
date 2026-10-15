import { PublicFilePreview } from './PublicFilePreview';
import { useEffect, useState } from 'react';
import { useParams } from 'react-router';
import { Button, Result, Skeleton } from 'antd';
import { MenuOutlined, PlusOutlined, CloseOutlined, CommentOutlined } from '@ant-design/icons';
import { apiRequest, unwrapData } from '../api';
import { setPublicChatCredential } from '../publicChatTransport';
import { useChatViewport } from '../hooks/useChatViewport';
import { useComposerFiles } from '../hooks/useComposerFiles';
import { getApiUrl } from '../api';
import { useModelCapabilitiesStore } from '../stores/modelCapabilitiesStore';
import { useChatStore } from '../stores/chatStore';
import { sessionToChatItem } from '../hooks/chatSessionMapping';
import { ChatArea } from '../components/chat/ChatArea';
import { ContentErrorBoundary, ImagePreview } from '../components/common';
import { PublicChatContext, type PublicChatInfo } from './PublicChatContext';
import { createPublicDraft, usePublicConversation } from './usePublicConversation';
import { t } from '../i18n';
import '../styles/standalone-chat.css';

const noExport = async () => {};
const noShare = async (): Promise<never> => { throw new Error('Unavailable'); };

function visitorSecret(token: string) {
  const key = `public-chat:visitor:${token}`;
  let value = localStorage.getItem(key);
  if (!value || !/^[A-Za-z0-9_-]{32,64}$/.test(value)) {
    value = Array.from(crypto.getRandomValues(new Uint8Array(24)), x => x.toString(16).padStart(2, '0')).join('');
    localStorage.setItem(key, value);
  }
  return value;
}

function PublicEntry({ token, kind }: { token: string; kind: string }) {
  const [info, setInfo] = useState<PublicChatInfo | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setPublicChatCredential({ token, visitor: visitorSecret(token) });
      const data = unwrapData<PublicChatInfo>(await apiRequest('/v1/public-chats/session'));
      if (cancelled) return;
      if (data.kind !== kind) throw new Error(t('链接不可用或已停用'));
      const st = useChatStore.getState();
      st.hydrateForUser(`public:${data.visitor_id}`);
      const chats = Object.fromEntries(data.chats.map(raw => {
        const chat = sessionToChatItem(raw);
        return [chat.id, { ...chat, projectName: data.kind === 'projects' ? data.name : undefined }];
      }));
      st.setStore({ chats, order: Object.keys(chats) });
      for (const id of Object.keys(chats)) st.addBackendSessionId(id);
      if (!useChatStore.getState().store.chats[useChatStore.getState().currentChatId]) createPublicDraft(data);
      await useModelCapabilitiesStore.getState().fetchCapabilities();
      if (cancelled) return;
      document.title = data.name;
      setInfo(data);
    })().catch(() => { if (!cancelled) setError(t('链接不可用或已停用')); });
    return () => { cancelled = true; };
  }, [token, kind, attempt]);
  if (error) return <Result status="warning" title={error} subTitle={t('请联系发布者确认链接，或稍后重试。')}
    extra={<Button onClick={() => { setInfo(null); setError(''); setAttempt(value => value + 1); }}>{t('重试')}</Button>} />;
  if (!info) return <div className="jx-publicLoading" aria-busy="true"><Skeleton active paragraph={{ rows: 3 }} /></div>;
  return <PublicChatContext.Provider value={info}><PublicWorkspace key={info.visitor_id} info={info} /></PublicChatContext.Provider>;
}

function PublicWorkspace({ info }: { info: PublicChatInfo }) {
  const st = useChatStore();
  const conversation = usePublicConversation(info);
  const files = useComposerFiles(getApiUrl());
  const [sidebar, setSidebar] = useState(false);
  const hasMessages = !!st.store.chats[st.currentChatId]?.messages.length;
  const { inputRef, fileInputRef, chatListRef, messagesEndRef, handleContentRef, userScrolledUpRef } =
    useChatViewport(st.currentChatId, hasMessages, true);
  const send = (text?: string) => {
    userScrolledUpRef.current = false;
    conversation.send(text);
  };
  const newChat = () => { st.newChat(); createPublicDraft(info); setSidebar(false); };
  return <div className="jx-appShell jx-publicChat">
    {sidebar && <button className="jx-publicBackdrop" onClick={() => setSidebar(false)} aria-label={t('关闭侧边栏')} />}
    <aside className={`jx-publicSidebar${sidebar ? ' is-open' : ''}`} aria-label={t('对话历史')}>
      <div className="jx-publicBrand"><CommentOutlined /><strong title={info.name}>{info.name}</strong>
        <Button className="jx-publicSidebarClose" type="text" icon={<CloseOutlined />} onClick={() => setSidebar(false)} aria-label={t('关闭侧边栏')} />
      </div>
      <Button className="jx-publicNewChat" icon={<PlusOutlined />} onClick={newChat}>{t('新对话')}</Button>
      <div className="jx-publicHistoryLabel">{t('对话历史')}</div>
      <nav className="jx-publicHistory">{st.store.order.map(id => <button key={id} type="button"
        className={id === st.currentChatId ? 'is-active' : ''} aria-current={id === st.currentChatId ? 'page' : undefined}
        title={st.store.chats[id]?.title} onClick={() => { st.setCurrentChatId(id); setSidebar(false); }}>
        <CommentOutlined /><span>{st.store.chats[id]?.title || t('新对话')}</span>
      </button>)}</nav>
      <p className="jx-publicHistoryNote">{t('对话记录仅在当前浏览器保留入口。')}</p>
    </aside>
    <main className="jx-publicMain jx-primaryPane is-chatSurface">
      <header className="jx-chatTopbar jx-publicTopbar">
        <Button className="jx-publicMenu" type="text" icon={<MenuOutlined />} onClick={() => setSidebar(true)} aria-label={t('打开侧边栏')} />
        <strong className="jx-chatTopbarTitle" title={info.name}>{info.name}</strong>
        <Button type="text" icon={<PlusOutlined />} onClick={newChat}>{t('新对话')}</Button>
      </header>
      <div className="jx-publicContent jx-content--chatSurface">
        {conversation.error ? <Result status="warning" title={conversation.error} extra={<Button onClick={conversation.retry}>{t('重试')}</Button>} />
          : conversation.loading ? <div className="jx-publicLoading"><Skeleton active paragraph={{ rows: 5 }} /></div>
          : <ContentErrorBoundary fallback={<Result status="warning" title={t('加载失败')} />}><ChatArea send={send} abort={conversation.abort}
              exportChatRecord={noExport} createChatShare={noShare} handleFileSelect={files.handleFileSelect} removeFile={files.removeFile}
              inputRef={inputRef} fileInputRef={fileInputRef} chatListRef={chatListRef} messagesEndRef={messagesEndRef} onViewportMount={handleContentRef} /></ContentErrorBoundary>}
      </div>
    </main>
    <ImagePreview />
    <PublicFilePreview />
  </div>;
}

export default function PublicChatApp() {
  const { resourceId = '', kind = '' } = useParams();
  return <PublicEntry key={`${kind}:${resourceId}`} token={resourceId} kind={kind} />;
}
