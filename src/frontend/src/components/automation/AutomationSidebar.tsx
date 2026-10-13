import { PlusOutlined } from '@ant-design/icons';
import { Button, message } from 'antd';
import { AnimatePresence } from 'motion/react';
import { useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router';
import { t } from '../../i18n';
import { useRouteSubs } from '../../routing/usePanel';
import { useAutomationStore } from '../../stores/automationStore';
import { useChatStore, useSidebarOrderStore } from '../../stores';
import { chatIdFromPath } from '../../routing/navigation';
import { buildAutomationSidebarEntries } from './automationSidebarEntries';
import { compareSidebarItems } from '../../utils/sidebarOrder';
import { ChatSidebarItem } from '../sidebar/ChatSidebarItem';
import type { SidebarProps } from '../sidebar/sidebarTypes';
import { startAutomationCreationInChat } from './startAutomationCreation';

export function AutomationSidebar({ actions }: { actions: SidebarProps }) {
  const setSelectedTaskId = useAutomationStore(s => s.setSelectedTaskId);
  const [starting, setStarting] = useState(false);
  const selected = useRouteSubs()[0];
  const selectedChatId = chatIdFromPath(useLocation().pathname);
  const store = useChatStore(s => s.store);
  const loading = useChatStore(s => s.chatsLoading);
  const manualOrder = useSidebarOrderStore(s => s.order);
  const dragScopeRef = useRef<string | null>(null);
  const conversations = useMemo(() => {
    const index = new Map(manualOrder.map((id, i) => [id, i]));
    return buildAutomationSidebarEntries(store.order.map(id => store.chats[id]).filter(Boolean))
      .sort((a, b) => compareSidebarItems(a.item, b.item, index));
  }, [store, manualOrder]);
  const groupItems = conversations.map(entry => entry.item);
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
        {conversations.map(({ item, targetChatId, chatIds }) => {
          const executionChatId = selectedChatId && chatIds.includes(selectedChatId) ? selectedChatId : targetChatId;
          return <ChatSidebarItem
          key={item.automationTaskId && item.automationTaskId !== 'new' ? `task:${item.automationTaskId}` : item.id}
          item={item} groupItems={groupItems} scopeKey="automation-conversations"
          dragScopeRef={dragScopeRef} statusChatIds={chatIds}
          active={item.automationTaskId && item.automationTaskId !== 'new'
            ? selected === item.automationTaskId : selectedChatId === item.id}
          actions={{
            ...actions,
            onSelectChat: () => actions.onSelectChat(targetChatId),
            onExportChat: () => actions.onExportChat(executionChatId),
            onDeleteChat: () => actions.onDeleteChat(executionChatId),
          }}
        />;
        })}
      </AnimatePresence>}
    </div>
  </div>;
}
