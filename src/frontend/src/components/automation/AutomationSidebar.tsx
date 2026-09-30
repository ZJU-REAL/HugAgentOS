import { PlusOutlined } from '@ant-design/icons';
import { Button, message } from 'antd';
import { AnimatePresence } from 'motion/react';
import { useMemo, useRef, useState } from 'react';
import { t } from '../../i18n';
import { useRouteSubs } from '../../routing/usePanel';
import { useAutomationStore } from '../../stores/automationStore';
import { useChatStore, useSidebarOrderStore } from '../../stores';
import { isAutomationHistoryChat } from '../../utils/history';
import { compareSidebarItems } from '../../utils/sidebarOrder';
import { ChatSidebarItem } from '../sidebar/ChatSidebarItem';
import type { SidebarProps } from '../sidebar/sidebarTypes';
import { startAutomationCreationInChat } from './startAutomationCreation';

export function AutomationSidebar({ actions }: { actions: SidebarProps }) {
  const setSelectedTaskId = useAutomationStore(s => s.setSelectedTaskId);
  const [starting, setStarting] = useState(false);
  const selected = useRouteSubs()[0];
  const store = useChatStore(s => s.store);
  const loading = useChatStore(s => s.chatsLoading);
  const manualOrder = useSidebarOrderStore(s => s.order);
  const dragScopeRef = useRef<string | null>(null);
  const conversations = useMemo(() => {
    const index = new Map(manualOrder.map((id, i) => [id, i]));
    return store.order.map(id => store.chats[id])
      .filter(chat => isAutomationHistoryChat(chat)
        && (chat.automationTaskId !== 'new' || chat.messages.length > 0))
      .sort((a, b) => compareSidebarItems(a, b, index));
  }, [store, manualOrder]);
  const start = async () => {
    setStarting(true);
    try { await startAutomationCreationInChat(); }
    catch (error) { message.error(error instanceof Error ? error.message : t('创建失败')); }
    finally { setStarting(false); }
  };
  return <div className="jx-taskSidebar">
    <Button aria-label={t('新建定时任务')} className="jx-taskCreateButton" type="text" icon={<PlusOutlined />} loading={starting} onClick={() => void start()}>{t('新建定时任务')}</Button>
    <button className={`jx-navItem${!selected ? ' active' : ''}`} onClick={() => setSelectedTaskId(null)}>{t('全部任务')}</button>
    <div className="jx-sidebarScrollArea">
      {loading ? <div className="jx-historySkeletonList" aria-busy="true" aria-label={t('加载中')}>
        {Array.from({ length: 5 }, (_, i) => <div key={i} className="jx-historyItem jx-historyItemSkeleton"><div className="jx-skeletonBlock jx-historySkLine" /></div>)}
      </div> : <AnimatePresence initial={false} mode="popLayout">
        {conversations.map(item => <ChatSidebarItem key={item.id} item={item} groupItems={conversations} scopeKey="automation-conversations" dragScopeRef={dragScopeRef} actions={actions} />)}
      </AnimatePresence>}
    </div>
  </div>;
}
