import { message } from 'antd';
import { useEffect } from 'react';
import { Outlet, useParams } from 'react-router';
import { getAutomation, getAutomationRuns, getSession } from '../api';
import App from '../App';
import { t } from '../i18n';
import { useChatStore } from '../stores';
import { useAuthStore } from '../stores/authStore';
import { useAutomationChatStore } from '../stores/automationChatStore';
import { isAddressableChat } from '../stores/chatStore';
import { navigateTo, pathForChat, pathForPanel, setConversationOwnerResolver } from './navigation';

setConversationOwnerResolver(id => useChatStore.getState().store.chats[id]?.automationTaskId);

/** App 是常驻外壳，路由子节点只负责「地址 → store」这一个方向的同步，自身不渲染内容，
 *  所以换路由不会重挂载整个聊天界面。
 *
 *  需要同步回来的只剩会话：面板直接从地址算（usePanel），
 *  没有第二份状态要对齐。会话不同——草稿故意不占地址（`/` 不指向任何一段具体对话），
 *  所以 currentChatId 带着地址给不出的信息，必须真的存一份。 */
export function Shell() {
  return (
    <>
      <App />
      <Outlet />
    </>
  );
}

/** 只在两边不一致时动手：首次进入、前进后退、store 自己推的那次导航跑同一段代码
 *  都收敛到同一结果，以后有人加 <Link> 或深链也不会漏同步。 */
export function ChatRoute() {
  const { chatId } = useParams();
  const taskId = useChatStore(s => chatId ? s.store.chats[chatId]?.automationTaskId : undefined);
  useEffect(() => { if (chatId && taskId) navigateTo(pathForChat(chatId), { replace: true }); }, [chatId, taskId]);
  useEffect(() => {
    const chat = useChatStore.getState();
    useAutomationChatStore.getState().exitAutomationChat();
    if (chatId) {
      if (chat.currentChatId !== chatId) chat.adoptChatFromUrl(chatId);
    } else if (isAddressableChat(chat.currentChatId)) {
      // 返回首页时恢复该入口的未发送草稿；已发送的首页草稿由 store 换成空白草稿。
      chat.resumeHomeChat();
    }
  }, [chatId]);
  return null;
}

/** Resolve session metadata before marking it loaded; task ownership comes from the backend. */
export function AutomationChatRoute() {
  const { taskId, chatId } = useParams();
  const userId = useAuthStore(s => s.authUser?.user_id);
  useEffect(() => {
    if (!taskId || !chatId || !userId) return;
    const current = useChatStore.getState();
    if (current.currentChatId !== chatId) current.adoptChatFromUrl(chatId);
    // Display the loading state while the explicit metadata request below resolves.
    current.addBackendSessionId(chatId);
    let cancelled = false;
    const restore = async () => {
      const session = await getSession(chatId);
      if (cancelled) return;
      const chat = useChatStore.getState();
      chat.updateStore(prev => ({ ...prev, chats: { ...prev.chats, [chatId]: { ...prev.chats[chatId], ...session, modeSlug: session.modeSlug ?? prev.chats[chatId]?.modeSlug, thinkingEffort: session.thinkingEffort ?? prev.chats[chatId]?.thinkingEffort, planModeActive: prev.chats[chatId]?.planModeActive ?? session.planModeActive, batchModeActive: prev.chats[chatId]?.batchModeActive ?? session.batchModeActive, workflowModeActive: prev.chats[chatId]?.workflowModeActive ?? session.workflowModeActive, projectId: session.projectId || prev.chats[chatId]?.projectId, projectName: prev.chats[chatId]?.projectName, messages: prev.chats[chatId]?.messages || session.messages } }, order: prev.order.includes(chatId) ? prev.order : [chatId, ...prev.order] }));
      chat.addBackendSessionId(chatId);
      if (chat.currentChatId === chatId) chat.syncCurrentChatMode();
      if (session.automationTaskId !== taskId) { navigateTo(pathForChat(chatId), { replace: true }); return; }
      if (chat.currentChatId !== chatId) chat.adoptChatFromUrl(chatId);
      if (taskId === 'new') {
        useAutomationChatStore.getState().exitAutomationChat();
        return;
      }
      const [task, runs] = await Promise.all([getAutomation(taskId), getAutomationRuns(taskId, 50)]);
      if (cancelled) return;
      useAutomationChatStore.setState({ activeGroup: { taskId, taskName: task.name || taskId, runs }, selectedRunId: runs.find(r => r.chat_id === chatId)?.run_id || null });
    };
    void restore().catch(e => {
      if (cancelled) return;
      message.error(e instanceof Error ? e.message : t('加载失败'));
      navigateTo(pathForPanel('automation', taskId === 'new' ? null : taskId), { replace: true });
    });
    return () => { cancelled = true; };
  }, [taskId, chatId, userId]);
  return null;
}
