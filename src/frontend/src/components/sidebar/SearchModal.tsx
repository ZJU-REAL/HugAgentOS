import { useMemo, useRef } from 'react';
import { Modal, Input, Select, Tooltip } from 'antd';
import type { InputRef } from 'antd';
import { t } from '../../i18n';
import {
  SearchOutlined, CloseOutlined, EditOutlined, PushpinFilled,
} from '@ant-design/icons';
import { useUIStore, useChatStore, useAutomationChatStore } from '../../stores';
import type { HistoryTimeFilter } from '../../stores/uiStore';
import { useCatalogStore } from '../../stores/catalogStore';
import {
  matchesTimeFilter, getHistoryGroupKey, isAutomationHistoryChat,
  buildSidebarChatItems,
} from '../../utils/history';
import { highlightKeyword } from '../../utils/highlight';
import { getAutomationRuns } from '../../api';
import type { ChatItem } from '../../types';
import type { SearchResultItem } from '../../api';

interface SearchModalProps {
  onNewChat: () => void;
  onSelectChat: (id: string) => void;
  onSelectSearchResult: (item: SearchResultItem) => void;
}

const TIME_OPTIONS = [
  { value: 'all', label: t('全部时间') },
  { value: 'today', label: t('今天') },
  { value: '7d', label: t('近 7 天') },
  { value: '30d', label: t('近 30 天') },
];

const TYPE_OPTIONS = [
  { value: 'all', label: t('全部') },
  { value: 'normal', label: t('普通对话') },
  { value: '_mode:agent', label: t('子智能体') },
  { value: '_mode:plan', label: t('计划模式') },
  { value: '_mode:automation', label: t('自动化') },
];

const GROUP_ORDER: Array<'today' | 'yesterday' | 'week' | 'month' | 'older'> =
  ['today', 'yesterday', 'week', 'month', 'older'];
const GROUP_LABELS: Record<string, string> = {
  today: t('今天'), yesterday: t('昨天'), week: t('近 7 天'), month: t('近 30 天'), older: t('更早'),
};

function matchesTypeFilter(item: ChatItem, type: string): boolean {
  if (type === 'all') return true;
  const isAuto = isAutomationHistoryChat(item);
  const isAgent = !!item.agentName;
  const isPlan = !!item.planChat;
  if (type === '_mode:automation') return isAuto;
  if (type === '_mode:agent') return isAgent;
  if (type === '_mode:plan') return isPlan;
  if (type === 'normal') return !isAuto && !isAgent && !isPlan;
  return true;
}

function ItemTypeIcon({ item }: { item: ChatItem }) {
  if (item.automationRun) {
    return (
      <Tooltip title={t('自动化任务')}>
        <span className="jx-searchItemTypeIcon" style={{ color: '#F8AB42', fontSize: 13 }}>&#9889;</span>
      </Tooltip>
    );
  }
  if (item.agentName) {
    return (
      <Tooltip title={item.agentName}>
        <img src="/home/new-icons/agent.svg" alt="" className="jx-searchItemTypeIcon" />
      </Tooltip>
    );
  }
  if (item.planChat) {
    return (
      <Tooltip title={t('计划模式')}>
        <img src="/home/new-icons/plan.svg" alt="" className="jx-searchItemTypeIcon" />
      </Tooltip>
    );
  }
  return <span className="jx-searchItemTypeIcon jx-searchItemTypeIcon--dot" aria-hidden="true" />;
}

export function SearchModal({ onNewChat, onSelectChat, onSelectSearchResult }: SearchModalProps) {
  // ── Fine-grained subscriptions, to avoid useUIStore() grabbing the whole object wholesale and re-rendering on every unrelated UI state change ──
  const searchModalOpen = useUIStore((s) => s.searchModalOpen);
  const closeSearchModal = useUIStore((s) => s.closeSearchModal);
  const searchKeyword = useUIStore((s) => s.searchKeyword);
  const setSearchKeyword = useUIStore((s) => s.setSearchKeyword);
  const searchResults = useUIStore((s) => s.searchResults);
  const searchLoading = useUIStore((s) => s.searchLoading);
  const historyTimeFilter = useUIStore((s) => s.historyTimeFilter);
  const setHistoryTimeFilter = useUIStore((s) => s.setHistoryTimeFilter);
  const historyTopicFilter = useUIStore((s) => s.historyTopicFilter);
  const setHistoryTopicFilter = useUIStore((s) => s.setHistoryTopicFilter);

  // store is a large object, triggered on every streaming token update; narrowing to order + chats references reduces re-renders
  const storeOrder = useChatStore((s) => s.store.order);
  const storeChats = useChatStore((s) => s.store.chats);
  const currentChatId = useChatStore((s) => s.currentChatId);
  const panel = useCatalogStore((s) => s.panel);
  const sidebarTasks = useAutomationChatStore((s) => s.sidebarTasks);
  const sidebarPrefs = useAutomationChatStore((s) => s.sidebarPrefs);
  const activeAutoTaskId = useAutomationChatStore((s) => s.activeGroup?.taskId);
  const enterAutomationChat = useAutomationChatStore((s) => s.enterAutomationChat);

  const inputRef = useRef<InputRef>(null);

  // Shared with Sidebar: keep order/shape exactly identical to avoid the two sides getting out of sync
  const allItems = useMemo<ChatItem[]>(
    () => buildSidebarChatItems({ chats: storeChats, order: storeOrder }, sidebarTasks, sidebarPrefs),
    [storeChats, storeOrder, sidebarTasks, sidebarPrefs],
  );

  const hasKeyword = !!searchKeyword.trim();
  const timeFilterActive = historyTimeFilter !== 'all';
  const typeFilterActive = historyTopicFilter !== 'all';
  // Flat mode: when there's a keyword or a time filter is applied; otherwise grouped by time.
  const flatMode = hasKeyword || timeFilterActive;

  // Browse list (no keyword): run filters over the local allItems
  const filteredBrowseItems = useMemo(() => {
    return allItems
      .filter((item) => matchesTimeFilter(item.updatedAt || item.createdAt, historyTimeFilter))
      .filter((item) => matchesTypeFilter(item, historyTopicFilter))
      .sort((a, b) => {
        const pinDiff = Number(!!b.pinned) - Number(!!a.pinned);
        if (pinDiff !== 0) return pinDiff;
        return (b.updatedAt || 0) - (a.updatedAt || 0);
      });
  }, [allItems, historyTimeFilter, historyTopicFilter]);

  // Search results must also be filtered (the keyword comes from App.tsx's debounced fetch)
  const filteredSearchResults = useMemo(() => {
    return searchResults
      .filter((item) => matchesTimeFilter(item.updatedAt || item.createdAt, historyTimeFilter))
      .filter((item) => matchesTypeFilter(item as ChatItem, historyTopicFilter));
  }, [searchResults, historyTimeFilter, historyTopicFilter]);

  // Grouping (only used when not in flatMode)
  const groupedItems = useMemo(() => {
    const groups: Record<string, ChatItem[]> = {
      today: [], yesterday: [], week: [], month: [], older: [],
    };
    filteredBrowseItems.forEach((item) => {
      const ts = item.updatedAt || item.createdAt || 0;
      groups[getHistoryGroupKey(ts)].push(item);
    });
    return GROUP_ORDER
      .map((key) => ({ key, label: GROUP_LABELS[key], items: groups[key] }))
      .filter((g) => g.items.length > 0);
  }, [filteredBrowseItems]);

  // Unified item click: automation items need to fetch runs first, regular items go through onSelectChat
  const handlePickItem = async (item: ChatItem) => {
    closeSearchModal();
    if (item.automationRun && item.automationTaskId) {
      try {
        const runs = await getAutomationRuns(item.automationTaskId, 50);
        enterAutomationChat(item.automationTaskId, item.title, runs);
      } catch { /* ignore */ }
    } else {
      onSelectChat(item.id);
    }
  };

  // Search hit: an automation hit must go through the same entry point as handlePickItem (fetch runs + enterAutomationChat),
  // otherwise it just materializes a chat shell (onSelectSearchResult).
  const handlePickSearchResult = (item: SearchResultItem) => {
    if (item.automationRun || item.automationTaskId) {
      void handlePickItem(item);
      return;
    }
    closeSearchModal();
    onSelectSearchResult(item);
  };

  const handleNewChat = () => {
    closeSearchModal();
    onNewChat();
  };

  // Show the new-chat entry: only when the user isn't searching (no keyword, no filter)
  const showNewChatRow = !hasKeyword && !timeFilterActive && !typeFilterActive;

  const renderItemRow = (
    item: ChatItem,
    opts?: {
      snippet?: { match_type?: string; matched_snippet?: string };
      onClick?: () => void;
    },
  ) => {
    // The "active state" of an automation virtual item is based on activeGroup.taskId, consistent with Sidebar
    const isActive = panel === 'chat' && (
      item.automationRun
        ? !!item.automationTaskId && activeAutoTaskId === item.automationTaskId
        : item.id === currentChatId
    );
    return (
      <div
        key={item.id}
        className={`jx-searchItem${isActive ? ' active' : ''}`}
        onClick={opts?.onClick ?? (() => void handlePickItem(item))}
      >
        {item.pinned && (
          <span className="jx-searchItemPin"><PushpinFilled /></span>
        )}
        <ItemTypeIcon item={item} />
        <div className="jx-searchItemMain">
          <span className="jx-searchItemTitle">
            {hasKeyword ? highlightKeyword(item.title || t('对话'), searchKeyword) : (item.title || t('对话'))}
          </span>
          {opts?.snippet?.match_type === 'content' && opts.snippet.matched_snippet && (
            <span className="jx-searchItemSnippet">
              {highlightKeyword(opts.snippet.matched_snippet, searchKeyword)}
            </span>
          )}
        </div>
      </div>
    );
  };

  // Render body
  // Each state's container carries a key: force a remount when switching between skeleton/results/empty states, so the CSS mount fade-in replays
  // (all branches are <div>, and without a key React reuses the node, preventing the animation from triggering).
  let body: React.ReactNode;
  if (hasKeyword && searchLoading) {
    body = (
      <div key="skeleton" className="jx-searchSkeletonList" aria-hidden="true">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="jx-searchSkeletonItem">
            <div className="jx-skeletonBlock jx-searchSkTitle" />
            <div className="jx-skeletonBlock jx-searchSkSnippet" />
          </div>
        ))}
      </div>
    );
  } else if (hasKeyword) {
    body = filteredSearchResults.length === 0 ? (
      <SearchEmptyState />
    ) : (
      <div key="flat-search" className="jx-searchFlatList">
        {filteredSearchResults.map((r) =>
          renderItemRow(r as ChatItem, {
            snippet: { match_type: r.match_type, matched_snippet: r.matched_snippet },
            // Search hits go through handleSelectSearchResult: it materializes the hit into the local store when necessary
            onClick: () => handlePickSearchResult(r),
          }),
        )}
      </div>
    );
  } else if (flatMode) {
    // No keyword + time filter active → flat
    body = filteredBrowseItems.length === 0 ? (
      <SearchEmptyState />
    ) : (
      <div key="flat-browse" className="jx-searchFlatList">
        {filteredBrowseItems.map((it) => renderItemRow(it))}
      </div>
    );
  } else {
    // No keyword + (no filter or type filter only) → grouped
    body = groupedItems.length === 0 ? (
      <SearchEmptyState />
    ) : (
      <div key="grouped" className="jx-searchGroupList">
        {groupedItems.map((g) => (
          <div key={g.key} className="jx-searchGroup">
            <div className="jx-searchGroupTitle">{g.label}</div>
            <div className="jx-searchGroupItems">
              {g.items.map((it) => renderItemRow(it))}
            </div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <Modal
      open={searchModalOpen}
      onCancel={closeSearchModal}
      footer={null}
      closable={false}
      width={640}
      maskClosable
      destroyOnHidden
      className="jx-searchModal"
      // Focus only after the enter transition finishes (replacing the original 80ms setTimeout magic number), to avoid focus stealing the transition
      afterOpenChange={(open) => { if (open) inputRef.current?.focus(); }}
      aria-label={t('搜索对话')}
      // Center the Modal slightly above middle (more like a command palette)
      style={{ top: 96 }}
    >
      <div className="jx-searchModalHeader">
        <SearchOutlined className="jx-searchModalIcon" />
        <Input
          ref={inputRef}
          variant="borderless"
          placeholder={t('搜索对话标题或内容…')}
          value={searchKeyword}
          onChange={(e) => setSearchKeyword(e.target.value)}
          className="jx-searchModalInput"
          onKeyDown={(e) => {
            // A Chinese IME commit (pinyin commit) also fires Enter, so composition must be let through first
            if (e.nativeEvent.isComposing || e.keyCode === 229) return;
            if (e.key === 'Enter' && showNewChatRow) {
              e.preventDefault();
              handleNewChat();
            }
          }}
        />
        <span className="jx-searchModalKbd" aria-label="快捷键">⌘K</span>
        <button
          type="button"
          className="jx-searchModalClose"
          onClick={closeSearchModal}
          aria-label={t('关闭')}
        >
          <CloseOutlined />
        </button>
      </div>

      <div className="jx-searchModalFilters">
        <span className="jx-searchModalFilterLabel">{t('时间')}</span>
        <Select
          variant="borderless"
          size="small"
          value={historyTimeFilter}
          onChange={(v) => setHistoryTimeFilter(v as HistoryTimeFilter)}
          options={TIME_OPTIONS}
          popupClassName="jx-searchModalFilterPopup"
          className="jx-searchModalFilterSelect"
          popupMatchSelectWidth={false}
        />
        <span className="jx-searchModalFilterDivider" aria-hidden="true" />
        <span className="jx-searchModalFilterLabel">{t('类型')}</span>
        <Select
          variant="borderless"
          size="small"
          value={historyTopicFilter}
          onChange={(v) => setHistoryTopicFilter(v)}
          options={TYPE_OPTIONS}
          popupClassName="jx-searchModalFilterPopup"
          className="jx-searchModalFilterSelect"
          popupMatchSelectWidth={false}
        />
      </div>

      <div className="jx-searchModalBody">
        {showNewChatRow && (
          <button type="button" className="jx-searchNewChatRow" onClick={handleNewChat}>
            <EditOutlined className="jx-searchNewChatIcon" />
            <span className="jx-searchNewChatText">{t('新建对话')}</span>
            <span className="jx-searchNewChatHint">↵</span>
          </button>
        )}
        {body}
      </div>
    </Modal>
  );
}

function SearchEmptyState() {
  return (
    <div className="jx-searchEmptyState">
      <SearchOutlined className="jx-searchEmptyIcon" />
      <div className="jx-searchEmptyTitle">{t('无匹配结果')}</div>
      <div className="jx-searchEmptyHint">{t('试试调整关键词或筛选条件')}</div>
    </div>
  );
}
