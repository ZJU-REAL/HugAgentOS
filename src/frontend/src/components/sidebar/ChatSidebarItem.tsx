import { DeleteOutlined, EditOutlined, EllipsisOutlined, ExportOutlined, PushpinFilled, PushpinOutlined, SortAscendingOutlined, StarFilled, StarOutlined } from '@ant-design/icons';
import { Dropdown, Input, Tooltip, message, type MenuProps } from 'antd';
import { motion } from 'motion/react';
import { useMemo, useRef, type DragEvent as ReactDragEvent } from 'react';
import { useIsMobileViewport } from '../../hooks/useIsMobileViewport';
import { t } from '../../i18n';
import { chatIdFromPath } from '../../routing/navigation';
import { usePanel } from '../../routing/usePanel';
import { useChatStore, useSidebarOrderStore, useUIStore } from '../../stores';
import type { ChatItem } from '../../types';
import { CHAT_REFERENCE_MIME } from '../../utils/constants';
import { conversationTitle } from '../../utils/conversationTitle';
import { formatDateTime } from '../../utils/date';
import { EASE, LAYOUT_ANIM_MAX_ITEMS } from '../../utils/motionTokens';
import type { SidebarProps } from './sidebarTypes';
const HISTORY_ITEM_ENTER = { duration: 0.22, ease: EASE.brandOut };
const HISTORY_ITEM_EXIT = { duration: 0.18, ease: EASE.exit };
export function ChatSidebarItem({ item, groupItems, scopeKey, dragScopeRef, actions }: {
  item: ChatItem; groupItems: ChatItem[]; scopeKey: string;
  dragScopeRef: ReturnType<typeof useRef<string | null>>; actions: SidebarProps;
}) {
  const { onSelectChat, onCommitRename, onStartRename, onTogglePinned, onToggleFavorite, onExportChat, onDeleteChat } = actions;
  const { editingChatId, setEditingChatId, editingTitle, setEditingTitle, pendingConfirm, pendingDesignPick, pendingUserQuestions } = useUIStore();
  const { sendingChatIds, remoteRunningChatIds } = useChatStore();
  const { draggingId, dropTarget, setDragging, setDropTarget, reorderWithinGroup, order: manualOrder, resetOrder: resetSidebarOrder } = useSidebarOrderStore();
  const panel = usePanel();
  const historyItemHeight = useIsMobileViewport() ? 44 : 36;
  const resetOrderMenuItems = useMemo<NonNullable<MenuProps['items']>>(() => (
    manualOrder.length === 0 ? [] : [
      {
        key: 'reset-order',
        label: t('恢复默认排序'),
        icon: <SortAscendingOutlined />,
        onClick: ({ domEvent }) => {
          domEvent.stopPropagation();
          resetSidebarOrder();
          message.success(t('已恢复默认排序'));
        },
      },
      { type: 'divider' as const },
    ]
  ), [manualOrder.length, resetSidebarOrder]);

  const listLen = groupItems.length;
  const isActive = (panel === 'chat' || panel === 'automation') && chatIdFromPath() === item.id;
  const isEditing = editingChatId === item.id;

  const handleClick = async () => {
    if (editingChatId && editingChatId !== item.id) { setEditingChatId(null); setEditingTitle(''); }
    onSelectChat(item.id);
  };

  // ── 手动拖拽排序 ──
  // 拖拽只在「同一分组 + 同一置顶带」内生效：置顶项恒排在组顶部（见 sortedHistoryList），
  // 允许把普通会话拖进置顶区只会被排序规则弹回去，不如直接不接这个落点。
  const dragScope = `${scopeKey}:${item.pinned ? 'pinned' : 'plain'}`;
  const isDragging = draggingId === item.id;
  const dropHint = draggingId && draggingId !== item.id && dropTarget?.id === item.id
    ? dropTarget.place
    : null;

  const handleDragStart = (e: ReactDragEvent<HTMLDivElement>) => {
    if (isEditing) { e.preventDefault(); return; }
    e.dataTransfer.effectAllowed = 'copyMove';
    // Firefox 要求 dragstart 里必须写入数据，否则整个拖拽不启动
    try { e.dataTransfer.setData('text/plain', item.id); } catch { /* 某些浏览器只读 */ }
    // 同一次拖拽再挂一份会话身份：拖到输入框上就是"引用这段会话"，拖回列表内仍是排序。
    // 定时任务分组不是一段可引用的对话，不挂这个类型。
    {
      try {
        e.dataTransfer.setData(CHAT_REFERENCE_MIME, JSON.stringify({
          chat_id: item.id,
          title: conversationTitle(item.title),
          message_count: item.messages?.length ?? 0,
          last_active_display: formatDateTime(item.updatedAt || Date.now()).slice(0, 16),
        }));
      } catch { /* 某些浏览器只读 */ }
    }
    dragScopeRef.current = dragScope;
    setDragging(item.id);
  };

  const handleDragOver = (e: ReactDragEvent<HTMLDivElement>) => {
    if (!draggingId || draggingId === item.id) return;
    if (dragScopeRef.current !== dragScope) return;
    // dragover 阶段读不到 dataTransfer 内容（安全限制），所以拖拽源信息走 ref/store
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const rect = e.currentTarget.getBoundingClientRect();
    const place: 'before' | 'after' = e.clientY < rect.top + rect.height / 2 ? 'before' : 'after';
    if (dropTarget?.id !== item.id || dropTarget.place !== place) {
      setDropTarget({ id: item.id, place });
    }
  };

  const handleDrop = (e: ReactDragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    const draggedId = draggingId;
    if (!draggedId || draggedId === item.id || dragScopeRef.current !== dragScope) {
      setDragging(null);
      return;
    }
    const scopeIds = groupItems
      .filter((i) => !!i.pinned === !!item.pinned)
      .map((i) => i.id);
    const place = dropTarget?.id === item.id ? dropTarget.place : 'before';
    reorderWithinGroup(scopeIds, draggedId, item.id, place);
    dragScopeRef.current = null;
  };

  const handleDragEnd = () => {
    dragScopeRef.current = null;
    setDragging(null);
  };

  return (
    <motion.div
      key={item.id}
      // 拖拽期间关掉布局动画：位移动画会和浏览器原生拖影抢同一段位移，看起来会抖
      layout={!draggingId && listLen <= LAYOUT_ANIM_MAX_ITEMS ? 'position' : false}
      initial={{ opacity: 0, height: 0, minHeight: 0 }}
      animate={{ opacity: 1, height: historyItemHeight, minHeight: historyItemHeight }}
      exit={{ opacity: 0, height: 0, minHeight: 0, transition: HISTORY_ITEM_EXIT }}
      transition={HISTORY_ITEM_ENTER}
      style={{ overflow: 'hidden' }}
      className={`jx-historyItem${isActive ? ' active' : ''}${isEditing ? ' editing' : ''}${isDragging ? ' jx-historyItem--dragging' : ''
        }${dropHint === 'before' ? ' jx-historyItem--dropBefore' : ''}${dropHint === 'after' ? ' jx-historyItem--dropAfter' : ''
        }`}
      draggable={!isEditing}
      // onDragStart / onDragEnd 被 motion 占作自家手势 API（不会透传到 DOM），
      // 原生 HTML5 拖拽只能挂 capture 变体；onDragOver / onDrop 不冲突，照常挂。
      onDragStartCapture={handleDragStart}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
      onDragEndCapture={handleDragEnd}
      onClick={handleClick}>
      {isEditing ? (
        <Input size="small" value={editingTitle} autoFocus
          onChange={(e) => setEditingTitle(e.target.value)}
          onPressEnter={() => void onCommitRename(item.id)}
          onBlur={() => void onCommitRename(item.id)}
          onClick={(e) => e.stopPropagation()}
          maxLength={30} className="jx-historyEditInput" />
      ) : (
        <div className="jx-historyMain">
          {item.pinned ? (
            <Tooltip title={t('已置顶')}>
              <span className="jx-historyPinIcon" onClick={(e) => e.stopPropagation()}>
                <PushpinFilled />
              </span>
            </Tooltip>
          ) : null}
          {item.agentName ? (
            <Tooltip title={item.agentName}>
              <img src="/home/new-icons/agent.svg" alt={t('智能体')} className="jx-historyTypeIcon jx-historyTypeIcon--agent" style={{ width: 14, height: 14 }} />
            </Tooltip>
          ) : item.planChat ? (
            <Tooltip title={t('计划模式')}>
              <img src="/home/new-icons/plan.svg" alt={t('计划模式')} className="jx-historyTypeIcon jx-historyTypeIcon--plan" style={{ width: 14, height: 14 }} />
            </Tooltip>
          ) : null}
          <span className="jx-historyTitle">
            {conversationTitle(item.title) || t('对话')}
          </span>
          {(pendingUserQuestions[item.id]?.length ?? 0) > 0 ? (
            <>
              <Tooltip title={t('等待你的回答')}>
                <span className="jx-historyQuestionDot" aria-hidden="true" />
              </Tooltip>
              <span className="jx-visuallyHidden">{t('等待你的回答')}</span>
            </>
          ) : (sendingChatIds.has(item.id) || remoteRunningChatIds.has(item.id)) ? (
            <Tooltip title={t('运行中')}>
              <span className="jx-historyRunningDot" />
            </Tooltip>
          ) : ((pendingConfirm[item.id]?.length ?? 0) > 0 || !!pendingDesignPick[item.id]) ? (
            <Tooltip title={t('有待确认的操作')}>
              <span className="jx-historyConfirmDot" />
            </Tooltip>
          ) : null}
        </div>
      )}
      <div className="jx-historyActions">
        <Dropdown menu={{
          items: [
            ...resetOrderMenuItems,
            { key: 'pin', label: item.pinned ? t('取消置顶') : t('置顶'), icon: item.pinned ? <PushpinFilled /> : <PushpinOutlined />, onClick: ({ domEvent }) => { domEvent.stopPropagation(); onTogglePinned(item.id); } },
            { key: 'fav', label: item.favorite ? t('取消收藏') : t('收藏'), icon: item.favorite ? <StarFilled /> : <StarOutlined />, onClick: ({ domEvent }) => { domEvent.stopPropagation(); onToggleFavorite(item.id); } },
            { key: 'rename', label: t('重命名'), icon: <EditOutlined />, onClick: ({ domEvent }) => { domEvent.stopPropagation(); onStartRename(item); } },
            { key: 'export', label: t('导出'), icon: <ExportOutlined />, onClick: ({ domEvent }) => { domEvent.stopPropagation(); onExportChat(item.id); } },
            { type: 'divider' as const },
            { key: 'delete', label: t('删除'), icon: <DeleteOutlined />, danger: true, onClick: ({ domEvent }) => { domEvent.stopPropagation(); onDeleteChat(item.id); } },
          ],
        }} trigger={['click']} placement="bottomRight" overlayClassName="jx-chatItemMenu">
          <button aria-label={t('更多操作')} className="jx-historyMoreBtn" onClick={(e) => e.stopPropagation()}><EllipsisOutlined /></button>
        </Dropdown>
      </div>
    </motion.div>
  );

}
