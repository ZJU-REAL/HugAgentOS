import { CaretDownOutlined } from '@ant-design/icons';
import { AnimatePresence } from 'motion/react';
import { useRef, useState } from 'react';
import { t } from '../../i18n';
import { useChatStore } from '../../stores';
import { ChatSidebarItem } from './ChatSidebarItem';
import { ProjectSidebarGroup } from './ProjectSidebarGroup';
import type { SidebarProps } from './sidebarTypes';
import { useSidebarHistory } from './useSidebarHistory';
export function ChatSidebar({ actions }: { actions: SidebarProps }) {
  const { projectGroups, groupedHistoryList } = useSidebarHistory();
  const loading = useChatStore(s => s.chatsLoading);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const dragScopeRef = useRef<string | null>(null);
  const groups = [{ key: 'projects', label: t('项目') }, ...groupedHistoryList];
  return <>
    <button className="jx-newChatBtn" onClick={actions.onNewChat}><img src="/home/new-chat.svg" alt="" className="jx-newChatIcon" /><span>{t('新建对话')}</span></button>
    <div className="jx-sidebarScrollArea">
      {loading && <div className="jx-historySkeletonList" aria-busy="true" aria-label={t('加载中')}>
        {Array.from({ length: 8 }, (_, i) => <div key={i} className="jx-historyItem jx-historyItemSkeleton"><div className="jx-skeletonBlock jx-historySkLine" /></div>)}
      </div>}
      {!loading && groups.map(group => <section key={group.key} className={`jx-historyGroup${collapsed[group.key] ? ' jx-historyGroup--collapsed' : ''}`} aria-label={group.label}>
        <button className="jx-historyGroupHeader" aria-expanded={!collapsed[group.key]} onClick={() => setCollapsed(v => ({ ...v, [group.key]: !v[group.key] }))}><span className="jx-historyGroupTitle">{group.label}</span><CaretDownOutlined /></button>
        <div className={`jx-expandWrap jx-historyGroupExpand${collapsed[group.key] ? '' : ' jx-expandWrap--open'}`}><div className="jx-historyGroupList">
          {group.key === 'projects' ? projectGroups.map(pg => <ProjectSidebarGroup key={pg.projectId} pg={pg} actions={actions} dragScopeRef={dragScopeRef} />) : <AnimatePresence initial={false} mode="popLayout">{groupedHistoryList[0].items.map(item => <ChatSidebarItem key={item.id} item={item} groupItems={groupedHistoryList[0].items} scopeKey="history" dragScopeRef={dragScopeRef} actions={actions} />)}</AnimatePresence>}
        </div></div>
      </section>)}
    </div>
  </>;
}
