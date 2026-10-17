import { UserManagementPanel } from '../src/edition-ee/config/UserManagementPanel';
import { useAuthStore } from '../src/stores/authStore';
import { ConfigProvider } from 'antd';
import { SystemModelPanel } from '../src/components/settings/SystemModelPanel';
import { ModelsEditor } from '../src/components/admin/ModelsEditor';
import { useEffect, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { InputArea } from '../src/components/chat/InputArea';
import { readComposer } from '../src/stores/composerStore';
import { useChatStore } from '../src/stores/chatStore';
import { richEditor } from '../src/components/chat/composerRichText';
import '../src/styles/variables.css';
import '../src/styles/common.css';
import '../src/styles/chat.css';

// Browser-only fixture: no production database or account.
const test = window as unknown as { voiceTest: Record<string, unknown> };
test.voiceTest = { sent: [], streams: [] };
useAuthStore.setState({authUser: {user_id:'owner', username:'owner', can_use_voice:true}});
useChatStore.setState({ currentUserId: 'owner', currentChatId: 'source', activeRuns: {}, sendingChatIds: new Set(), sending: false,
  store: { order: ['source', 'other'], chats: Object.fromEntries(['source', 'other'].map(id => [id, { id, title: id, createdAt: 1, updatedAt: 1, messages: [], runTarget: 'cloud' }])) } });
export function Fixture() {
  const inputRef = useRef<HTMLTextAreaElement>(null), fileRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    test.voiceTest.permission = (allowed: boolean) => useAuthStore.setState({authUser:{user_id:'owner',username:'owner',can_use_voice:allowed}});
    test.voiceTest.text = () => readComposer().input;
    test.voiceTest.set = (text: string) => readComposer().setInput(text);
    test.voiceTest.selection = () => richEditor(inputRef.current!)?.state.selection.from;
    test.voiceTest.cursor = (pos: number) => richEditor(inputRef.current!)?.commands.setTextSelection(pos);
    test.voiceTest.switch = () => useChatStore.setState({ currentChatId: 'other' });
    test.voiceTest.undo = () => richEditor(inputRef.current!)?.commands.undo();
    test.voiceTest.attachment = () => readComposer().patch({ uploadedFiles: [new File(['hello'], 'kept.txt', { type: 'text/plain' })] });
  }, []);
  return <main style={{ padding: 24, maxWidth: 850, margin: '40px auto' }}>
    <InputArea inputRef={inputRef} fileInputRef={fileRef} send={() => { (test.voiceTest.sent as string[]).push(readComposer().input); readComposer().setInput(''); }} handleFileSelect={() => {}} removeFile={() => {}} />
  </main>;
}
createRoot(document.getElementById('root')!).render(<ConfigProvider theme={{ token: { motion: false } }}>{location.pathname === '/user-permissions' ? <UserManagementPanel token="fixture-only" /> : location.pathname === '/model-settings' ? <SystemModelPanel /> : location.pathname === '/model-admin' ? <ModelsEditor token="fixture-only" /> : <Fixture />}</ConfigProvider>);
