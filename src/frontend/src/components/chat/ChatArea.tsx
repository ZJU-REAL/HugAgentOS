import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Modal, Select, message } from 'antd';
import { FileTextOutlined, PieChartOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { AnimatePresence, motion } from 'motion/react';
import { EASE, staggerStyle } from '../../utils/motionTokens';
import { useDelayedFlag } from '../../hooks';
import {
  SCROLL_TO_BOTTOM_BTN_THRESHOLD,
  distanceFromBottom,
  scrollElementToBottom,
} from '../../utils/scroll';
import { useChatStore, useBatchStore, useEditionStore, usePluginUiStore, useUIStore } from '../../stores';
import { resolveText } from '../../plugin-ui';
import { useCatalogStore } from '../../stores/catalogStore';
import { useAgentStore } from '../../stores/agentStore';
import { usePageConfig } from '../../hooks/usePageConfig';
import { loadOlderMessages } from '../../hooks/useChatInit';
import { useStableCallback } from '../../hooks/useStableCallback';
import { t } from '../../i18n';
import { resolveBatchModeActive } from '../../utils/chatMode';

// Enter/exit animation params for the back-to-bottom button (module-level constants — ChatArea
// re-renders frequently with the message stream, so avoid rebuilding the object inside the render body)
const SCROLL_BTN_INITIAL = { opacity: 0, y: 4 };
const SCROLL_BTN_ANIMATE = { opacity: 1, y: 0 };
const SCROLL_BTN_EXIT = { opacity: 0, y: 6, transition: { duration: 0.12, ease: EASE.exit } };
const SCROLL_BTN_TRANSITION = { duration: 0.2, ease: EASE.brandOut };

import { MessageBubble } from './MessageBubble';

const HOME_SUGGESTION_ICONS = [SearchOutlined, FileTextOutlined, PieChartOutlined] as const;
const HOME_SUGGESTIONS_PER_PAGE = 3;
import { InputArea } from './InputArea';
import { PlanProgressStrip } from './PlanProgressStrip';
import { JobProgressStrip } from './JobProgressStrip';
import { FileConfirmBar } from './FileConfirmBar';
import { DesignPickerCard } from './DesignPickerCard';
import { AskUserQuestionComposer } from './AskUserQuestionComposer';
import { ChatShareBanner } from './ChatShareBanner';
import { getChatDetail } from '../../api';
import { chatAccessLevel } from '../../chatEdition';
import { BatchProgressPanel } from '../batch';
import { ContentErrorBoundary } from '../common';

/** Render any active or recently-finished batch plans associated with the
 *  current chat. Plans without a chat_id (legacy) match any chat so the
 *  user still sees their progress. */
function BatchPanelsForChat({ chatId }: { chatId: string }) {
  const plans = useBatchStore((s) => s.plans);
  const items = Object.values(plans).filter((p) => {
    if (p.status === 'awaiting_confirm') return false;
    const planChat = p.meta.chat_id;
    return !planChat || planChat === chatId;
  });
  if (items.length === 0) return null;
  return (
    <>
      {items.map((p) => (
        <BatchProgressPanel key={p.meta.plan_id} planId={p.meta.plan_id} />
      ))}
    </>
  );
}

interface ChatAreaProps {
  send: (text?: string) => void;
  abort?: () => void;
  activateQueuedMessage?: (chatId?: string) => Promise<void>;
  discardQueuedMessage?: (chatId?: string) => Promise<void>;
  continueLoop?: (chatId?: string) => void;
  exportChatRecord: (id: string) => Promise<void>;
  createChatShare: (
    id: string,
    selectedTs: number[],
    expiryOption: '3d' | '15d' | '3m' | 'permanent'
  ) => Promise<{ share_id: string; preview_url: string; expires_at?: string | null; expiry_option: '3d' | '15d' | '3m' | 'permanent' }>;
  handleFileSelect: (e: React.ChangeEvent<HTMLInputElement>, ref: React.RefObject<HTMLInputElement | null>) => void;
  removeFile: (index: number) => void;
  regenerate?: (messageIndex: number) => void;
  editAndResend?: (messageIndex: number, newContent: string) => void;
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  chatListRef: React.RefObject<HTMLDivElement | null>;
  messagesEndRef: React.RefObject<HTMLDivElement | null>;
}

export function ChatArea({
  send, abort, activateQueuedMessage, discardQueuedMessage, continueLoop, exportChatRecord, createChatShare, handleFileSelect, removeFile,
  regenerate, editAndResend,
  inputRef, fileInputRef, chatListRef, messagesEndRef,
}: ChatAreaProps) {
  type ShareExpiryOption = '3d' | '15d' | '3m' | 'permanent';
  const shareExpiryOptions = [
    { value: '3d', label: t('3天') },
    { value: '15d', label: t('15天') },
    { value: '3m', label: t('3个月') },
    { value: 'permanent', label: t('长期') },
  ] as const;
  const {
    store, currentChatId, setInput, planMode,
    shareSelectionMode, selectedShareMessageTs,
    pendingScrollMessageTs, setPendingScrollMessageTs,
    clearShareSelection,
    chatsLoading,
    backendSessionIds, loadedMsgIds,
    compactionNotices, dismissCompactionNotice,
  } = useChatStore();
  const [shareExpiryOption, setShareExpiryOption] = useState<ShareExpiryOption>('15d');
  const [shareExpiryModalOpen, setShareExpiryModalOpen] = useState(false);
  const [creatingShare, setCreatingShare] = useState(false);
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);
  const [suggestionPage, setSuggestionPage] = useState(0);
  // Shared-session access level: read-only sessions disable the input box.
  const [shareAccessLevel, setShareAccessLevel] = useState<'admin' | 'edit' | 'read' | null>(null);
  const pendingShareExpiryRef = useRef<ShareExpiryOption>('15d');

  const chat = store.chats[currentChatId];
  const hasPendingUserQuestion = useUIStore(
    (state) => (state.pendingUserQuestions[currentChatId]?.length ?? 0) > 0,
  );
  useEffect(() => {
    const content = document.querySelector<HTMLElement>('.jx-content');
    if (!content) return;
    const handleScroll = () => {
      setShowScrollToBottom(distanceFromBottom(content) > SCROLL_TO_BOTTOM_BTN_THRESHOLD);
    };
    handleScroll();
    content.addEventListener('scroll', handleScroll, { passive: true });
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(handleScroll) : null;
    ro?.observe(content);
    return () => {
      content.removeEventListener('scroll', handleScroll);
      ro?.disconnect();
    };
  }, []);

  // 这四个回调在上层是每次渲染重新定义的普通函数。不稳住身份，MessageBubble 的
  // memo 就完全不起作用——模型每吐一个字，整条对话的所有气泡都要重渲染一遍。
  const stableSend = useStableCallback(send);
  const stableExportChatRecord = useStableCallback(exportChatRecord);
  const stableRegenerate = useStableCallback(regenerate);
  const stableEditAndResend = useStableCallback(editAndResend);

  // ── 更早的消息：滚到顶自动续拉 ──────────────────────────────────────
  // 打开会话只铺最近一屏（见 useChatInit 的 MESSAGE_PAGE_SIZE）。这里在列表最上方
  // 放一个哨兵，滚进视野就续拉一页拼到前面。拼完必须把滚动位置补回去，否则新插入
  // 的内容会把用户正在看的那一段整个顶走。
  const olderSentinelRef = useRef<HTMLDivElement>(null);
  const messagePaging = useChatStore((s) => s.messagePaging[currentChatId]);
  const hasOlderMessages = !!messagePaging?.hasOlder;
  useEffect(() => {
    const sentinel = olderSentinelRef.current;
    const scroller = document.querySelector<HTMLElement>('.jx-content');
    if (!sentinel || !scroller || !hasOlderMessages) return;
    let disposed = false;
    const observer = new IntersectionObserver(async (entries) => {
      if (disposed || !entries.some((entry) => entry.isIntersecting)) return;
      const heightBefore = scroller.scrollHeight;
      const added = await loadOlderMessages(currentChatId);
      if (disposed) return;
      if (added > 0) {
        requestAnimationFrame(() => {
          if (!disposed) scroller.scrollTop += scroller.scrollHeight - heightBefore;
        });
      }
      // 观察器只在"进出视野"的跳变时回调。续拉一页后哨兵往往仍留在视野里
      // （第一屏不够高、或用户还停在顶部），不重新 observe 就再也不会触发，
      // 表现成"滚到顶卡住不再加载"。重新挂一次会立刻按当前状态回调一次。
      observer.unobserve(sentinel);
      observer.observe(sentinel);
    }, { root: scroller, rootMargin: '200px 0px 0px 0px' });
    observer.observe(sentinel);
    return () => {
      disposed = true;
      observer.disconnect();
    };
  }, [currentChatId, hasOlderMessages]);

  const scrollToBottom = () => {
    const content = document.querySelector<HTMLElement>('.jx-content');
    if (content) {
      scrollElementToBottom(content, true);
    } else {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }
  };

  const applyQuickScenario = (prompt: string) => {
    setInput(prompt);
    inputRef.current?.focus();
  };

  useEffect(() => {
    useChatStore.getState().clearShareSelection();
    // 引用追问不再在这里清空：它已随对话记录持久化（ChatItem.pendingQuote），
    // 切对话时由 chatStore 的各切换路径恢复/清空。
  }, [currentChatId]);

  // Stable identity on purpose: ChatShareBanner rebuilds its loader whenever this
  // prop changes and refetches on that loader, so an inline arrow here made the
  // banner re-request GET /v1/chats/{id} on *every* ChatArea render. During
  // streaming that is many renders a second, and the duplicate requests saturate
  // the browser's six-connection HTTP/1.1 budget — the next message's SSE request
  // then sat stalled for up to ~3.2s before it was even put on the wire.
  const handleShareLevelChange = useCallback(
    (lvl: 'admin' | 'edit' | 'read' | null) => setShareAccessLevel(lvl),
    [],
  );

  // Proactively fetch the access level when switching sessions — even if ChatShareBanner
  // is not mounted because it hit the hasNoMessages early-return branch, we still need to know the read level to hide the input box.
  useEffect(() => {
    if (!currentChatId) {
      setShareAccessLevel(null);
      return;
    }
    let aborted = false;
    void (async () => {
      try {
        const d = await getChatDetail(currentChatId);
        if (!aborted) setShareAccessLevel(chatAccessLevel(d));
      } catch {
        if (!aborted) setShareAccessLevel(null);
      }
    })();
    return () => { aborted = true; };
  }, [currentChatId]);

  useEffect(() => {
    if (!pendingScrollMessageTs) return;
    if (!chat?.messages.some((message) => message.ts === pendingScrollMessageTs)) return;

    const timer = window.setTimeout(() => {
      const target = document.querySelector<HTMLElement>(`[data-message-ts="${pendingScrollMessageTs}"]`);
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        // Locate-and-highlight: one-shot flash (jx-anim-flash primitive).
        // Replay is done via remove → force reflow → add; a leftover class is harmless (the animation plays only once),
        // so no animationend cleanup is needed.
        target.classList.remove('jx-anim-flash');
        void target.offsetWidth;
        target.classList.add('jx-anim-flash');
      }
      setPendingScrollMessageTs(null);
    }, 120);

    return () => window.clearTimeout(timer);
  }, [chat?.messages, pendingScrollMessageTs, setPendingScrollMessageTs]);

  const isCE = useEditionStore((s) => s.edition === 'ce');
  const pluginContributions = usePluginUiStore((s) => s.items);
  // 首页快捷入口只保留插件贡献的入口（管理端配置项已下线，场景引导统一走首页建议问题）。
  const pluginShortcuts = useMemo(() => {
    if (isCE) return [];
    return pluginContributions
      .flatMap((item) => item.contributes.shortcuts || [])
      .filter((shortcut) => !!shortcut.prompt)
      .map((shortcut) => ({
        id: shortcut.id,
        label: resolveText(shortcut.label, shortcut.id),
        icon: shortcut.icon || '',
        prompt: resolveText(shortcut.prompt!),
      }));
  }, [isCE, pluginContributions]);

  // ── Resolve sub-agent details for welcome page ──
  const { agents } = useAgentStore();
  const agentDetail = useMemo(() => {
    const aid = chat?.agentId;
    if (!aid) return null;
    return agents.find((a) => a.agent_id === aid) || null;
  }, [chat?.agentId, agents]);

  // ── Page config default values ──
  const cfgHeroTitle = usePageConfig('branding.hero_title', '你好，我是 HugAgentOS');
  const cfgHeroSubtitle = usePageConfig('branding.hero_subtitle', '今天想从哪里开始？');
  const cfgProductName = usePageConfig('branding.product_name', 'HugAgentOS');
  const cfgDisclaimer = usePageConfig('branding.disclaimer', '');
  const cfgInputPlaceholder = usePageConfig('texts.input_placeholder', '请输入你的问题，按Enter发送，Shift+Enter换行');
  const showHomepageLogo = usePageConfig('homepage.show_logo', true);
  const homepageLogoUrl = usePageConfig('homepage.logo_url', '/icon.png');
  const showHomepageSuggestions = usePageConfig('homepage.show_suggestions', true);
  const configuredHomepageSuggestions = usePageConfig<string[]>('homepage.suggested_questions', []);
  const homepageSuggestions = useMemo(
    () => showHomepageSuggestions
      ? configuredHomepageSuggestions.filter((prompt) => typeof prompt === 'string' && prompt.trim())
      : [],
    [configuredHomepageSuggestions, showHomepageSuggestions],
  );
  const suggestionPageCount = Math.max(
    1,
    Math.ceil(homepageSuggestions.length / HOME_SUGGESTIONS_PER_PAGE),
  );
  const visibleHomepageSuggestions = useMemo(() => {
    const start = (suggestionPage % suggestionPageCount) * HOME_SUGGESTIONS_PER_PAGE;
    return homepageSuggestions.slice(start, start + HOME_SUGGESTIONS_PER_PAGE);
  }, [homepageSuggestions, suggestionPage, suggestionPageCount]);

  useEffect(() => {
    setSuggestionPage(0);
  }, [configuredHomepageSuggestions, showHomepageSuggestions]);

  // ── Resolve hero text: sub-agent uses its own name/description ──
  const isAgentChat = !!(chat?.agentId);
  const isSiteChat = !!chat?.siteChat;
  const heroTitle = isSiteChat
    ? t('我们该构建什么？')
    : isAgentChat ? (chat.agentName || t('智能体')) : cfgHeroTitle;
  const heroSubtitle = isSiteChat
    ? t('描述你想要的网站，AI 将为你生成并一键发布上线')
    : isAgentChat
      ? (agentDetail?.description || agentDetail?.welcome_message || t('专业智能体'))
      : cfgHeroSubtitle;
  const suggestedQuestions = isAgentChat ? (agentDetail?.suggested_questions || []) : [];
  const isBatchChat = resolveBatchModeActive(chat);
  const inputPlaceholder = isSiteChat
    ? t('描述你想要的网站，例如：一个展示咖啡馆菜单与营业时间的单页网站')
    : isAgentChat
      ? t('向{name}提问...', { name: chat.agentName || t('智能体') })
      : isBatchChat
        ? t('描述要批量处理的对象与任务，例如："分别用一句话评价阿里、腾讯、字节"')
        : cfgInputPlaceholder;

  const hasNoMessages = !chat || chat.messages.length === 0;

  // Show a spinner when:
  // 1. The session list is still being fetched (initial page load), OR
  // 2. The current chat exists on the backend but its messages haven't arrived
  //    yet — covers both "not started" and "in-flight" states.  Without this
  //    the home page flashes every time the user clicks a history item.
  // Once the load completed (loadedMsgIds) an empty chat is genuinely empty —
  // fall through to the normal empty state instead of a forever-skeleton.
  const isMessagesLoading = hasNoMessages
    && backendSessionIds.has(currentChatId)
    && !loadedMsgIds.has(currentChatId);
  const messagesPending = hasNoMessages && (chatsLoading || isMessagesLoading);
  // Delayed skeleton screen: loads shorter than showAfter skip the skeleton entirely, avoiding flicker on fast hits
  const showChatSkeleton = useDelayedFlag(messagesPending);
  if (messagesPending) {
    if (!showChatSkeleton) {
      return <div className="jx-emptyPage jx-chatSkeleton" />;
    }
    return (
      <div className="jx-emptyPage jx-chatSkeleton">
        <div className="jx-chatSkeletonCenter">
          <div className="jx-chatSkeletonHero">
            <div className="jx-skeletonBlock jx-chatSkeletonTitle" />
            <div className="jx-skeletonBlock jx-chatSkeletonSubtitle" />
          </div>
          <div className="jx-skeletonBlock jx-chatSkeletonInput" />
          <div className="jx-chatSkeletonCards">
            {[1, 2, 3, 4, 5, 6].map((i) => (
              <div key={i} className="jx-skeletonBlock jx-chatSkeletonCard" />
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (hasNoMessages) {
    return (
      <div className={`jx-emptyPage${!isAgentChat && !isSiteChat ? ' jx-emptyPage--main' : ''}`}>
        {isSiteChat && (
          <div className="jx-siteHeroTop">
            <button
              type="button"
              className="jx-siteHeroTopBtn"
              onClick={() => {
                useCatalogStore.getState().setPanel('sites');
              }}
            >
              {t('我的站点')}
            </button>
          </div>
        )}
        <div className="jx-emptyCenter jx-anim-stagger">
          <div className="jx-heroBg" style={staggerStyle(0)}>
            {!isAgentChat && !isSiteChat && showHomepageLogo && homepageLogoUrl && (
              <img
                src={homepageLogoUrl}
                alt={`${cfgProductName} Logo`}
                className="jx-homeBrandMark"
              />
            )}
            <h1 className="jx-heroTitle">{heroTitle}</h1>
            <p className="jx-heroSubtitle">{heroSubtitle}</p>
            {!isAgentChat && !isSiteChat && (
              <div className="jx-mobileHeroText">
                <h1>HugAgentOS</h1>
                <p>{t('你的智能任务助手')}</p>
              </div>
            )}
          </div>

          <div className="jx-homeInput" style={staggerStyle(1)}>
            {shareAccessLevel === 'read' ? (
              <div className="jx-chatShareReadonly">
                {t('该会话由创建者设为只读共享，无法在此发送消息')}
              </div>
            ) : (
              <InputArea
                inputRef={inputRef}
                fileInputRef={fileInputRef}
                send={() => send()}
                abort={abort}
                activateQueuedMessage={activateQueuedMessage}
                discardQueuedMessage={discardQueuedMessage}
                continueLoop={continueLoop}
                handleFileSelect={handleFileSelect}
                removeFile={removeFile}
                placeholder={inputPlaceholder}
                mobilePlaceholder={!isAgentChat && !isSiteChat ? t('输入问题或需求') : inputPlaceholder}
                disableMention={isAgentChat}
              />
            )}
          </div>

          {/* Quick pills: only sub-agents show suggested questions */}
          {isAgentChat && suggestedQuestions.length > 0 && (
            <div className="jx-quickPills" style={staggerStyle(2)}>
              {suggestedQuestions.map((prompt: string) => (
                <button key={prompt} className="jx-quickPill" onClick={() => applyQuickScenario(prompt)}>
                  {prompt}
                </button>
              ))}
            </div>
          )}

          {!isAgentChat && !isSiteChat && visibleHomepageSuggestions.length > 0 && (
            <div className="jx-homeSuggestions" style={staggerStyle(2)}>
              <div className="jx-homeSuggestionList">
                {visibleHomepageSuggestions.map((prompt, idx) => {
                  const SuggestionIcon = HOME_SUGGESTION_ICONS[idx % HOME_SUGGESTION_ICONS.length];
                  return (
                    <button
                      key={prompt}
                      type="button"
                      className="jx-homeSuggestion"
                      onClick={() => applyQuickScenario(prompt)}
                    >
                      <SuggestionIcon className="jx-homeSuggestionIcon" aria-hidden="true" />
                      <span>{t(prompt)}</span>
                    </button>
                  );
                })}
              </div>
              {suggestionPageCount > 1 && (
                <button
                  type="button"
                  className="jx-homeSuggestionRefresh"
                  onClick={() => setSuggestionPage((page) => (page + 1) % suggestionPageCount)}
                  aria-label={t('换一批')}
                >
                  <ReloadOutlined aria-hidden="true" />
                  <span>{t('换一批')}</span>
                </button>
              )}
            </div>
          )}

          {/* Capability cards: plugin-contributed homepage entries (main agent page only) */}
          {!isAgentChat && !isSiteChat && pluginShortcuts.length > 0 && (
            <div className="jx-capCards" style={staggerStyle(2)}>
              {pluginShortcuts.map((card) => (
                <button
                  key={card.id}
                  type="button"
                  className="jx-capCard"
                  onClick={() => applyQuickScenario(card.prompt)}
                >
                  {card.icon ? <img src={card.icon} alt="" className="jx-capCardIcon" /> : null}
                  <span className="jx-capCardLabel">{t(card.label)}</span>
                </button>
              ))}
            </div>
          )}

        </div>
        {!isAgentChat && cfgDisclaimer && cfgDisclaimer.trim() && (
          <div className="jx-aiDisclaimer">
            {cfgDisclaimer.split('\n').map((line, i, arr) => (
              <span key={i}>{line}{i < arr.length - 1 ? <br /> : null}</span>
            ))}
          </div>
        )}
      </div>
    );
  }

  const handleCreateShare = async () => {
    if (selectedShareMessageTs.size === 0) {
      message.warning(t('请先选择要分享的对话记录'));
      return;
    }

    pendingShareExpiryRef.current = shareExpiryOption;
    setShareExpiryModalOpen(true);
  };

  const confirmCreateShare = async () => {
    if (selectedShareMessageTs.size === 0) {
      message.warning(t('请先选择要分享的对话记录'));
      return;
    }

    const selectedExpiryOption = pendingShareExpiryRef.current;
    setCreatingShare(true);
    try {
      const result = await createChatShare(currentChatId, Array.from(selectedShareMessageTs), selectedExpiryOption);
      const targetUrl = new URL(result.preview_url, window.location.origin).toString();
      window.open(targetUrl, '_blank', 'noopener');
      message.success(t('分享链接已生成'));
      setShareExpiryModalOpen(false);
      clearShareSelection();
    } catch (error) {
      message.error(error instanceof Error ? error.message : t('生成分享链接失败'));
    } finally {
      setCreatingShare(false);
    }
  };

  return (
    <div className="jx-chatWrap">
      <Modal
        title={t('有效期设置')}
        open={shareExpiryModalOpen}
        onOk={() => { void confirmCreateShare(); }}
        onCancel={() => {
          if (!creatingShare) {
            setShareExpiryModalOpen(false);
          }
        }}
        okText={t('生成链接')}
        cancelText={t('取消')}
        confirmLoading={creatingShare}
        destroyOnClose
      >
        <div style={{ display: 'grid', gap: 12 }}>
          <span>{t('请选择分享链接的有效时间')}</span>
          <Select
            value={shareExpiryOption}
            onChange={(value) => {
              const nextValue = value as ShareExpiryOption;
              setShareExpiryOption(nextValue);
              pendingShareExpiryRef.current = nextValue;
            }}
            options={shareExpiryOptions.map((option) => ({ value: option.value, label: option.label }))}
          />
        </div>
      </Modal>
      <AnimatePresence initial={false}>
        {shareSelectionMode && (
          <motion.div
            key="shareSelectionBar"
            className="jx-shareSelectionBar"
            style={{ overflow: 'hidden' }}
            initial={{ height: 0, opacity: 0, paddingTop: 0, paddingBottom: 0 }}
            animate={{ height: 'auto', opacity: 1, paddingTop: 14, paddingBottom: 14 }}
            exit={{ height: 0, opacity: 0, paddingTop: 0, paddingBottom: 0 }}
            transition={{ duration: 0.2, ease: EASE.standard }}
          >
            <div className="jx-shareSelectionInfo">
              <span className="jx-shareSelectionTitle">{t('分享记录选择')}</span>
              <span className="jx-shareSelectionCount">{t('已选择 {n} 条记录', { n: selectedShareMessageTs.size })}</span>
            </div>
            <div className="jx-shareSelectionActions">
              <button className="jx-shareSelectionSecondaryBtn" onClick={() => clearShareSelection()}>
                {t('取消')}
              </button>
              <button
                className="jx-shareSelectionPrimaryBtn"
                onClick={() => { void handleCreateShare(); }}
                disabled={selectedShareMessageTs.size === 0}
              >
                {t('生成分享链接')}
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
      <ChatShareBanner
        chatId={currentChatId}
        onLevelChange={handleShareLevelChange}
      />
      {!!compactionNotices[currentChatId] && (
        <div className="jx-compactionNotice" role="status">
          <span className="jx-compactionNotice-text">
            {t('对话较长，已自动压缩较早的上下文。多次压缩可能影响回答准确性，建议适时开启新对话。')}
          </span>
          <button
            type="button"
            className="jx-compactionNotice-close"
            aria-label={t('关闭')}
            onClick={() => dismissCompactionNotice(currentChatId)}
          >
            ×
          </button>
        </div>
      )}
      <div className="jx-chatList" ref={chatListRef}>
        {messagePaging?.hasOlder && (
          <div className="jx-olderLoader" ref={olderSentinelRef} role="status">
            {messagePaging.loading ? t('正在加载更早的消息…') : t('向上滚动查看更早的消息')}
          </div>
        )}
        {(chat.messages || []).map((m, idx) => (
          <ContentErrorBoundary
            key={m.ts}
            resetKey={`${currentChatId}:${m.ts}`}
            fallback={(
              <div className="jx-messageRenderError" role="alert">
                {t('这条消息包含无法显示的旧格式数据，已跳过异常内容。')}
              </div>
            )}
          >
            <MessageBubble
              m={m}
              messageIndex={idx}
              currentChatId={currentChatId}
              send={stableSend}
              exportChatRecord={stableExportChatRecord}
              regenerate={stableRegenerate}
              editAndResend={stableEditAndResend}
            />
          </ContentErrorBoundary>
        ))}
        <BatchPanelsForChat chatId={currentChatId} />
        <div ref={messagesEndRef} />
      </div>
      <AnimatePresence initial={false}>
        {showScrollToBottom && (
          <motion.button
            key="scrollToBottom"
            type="button"
            className="jx-scrollToBottomBtn"
            onClick={scrollToBottom}
            aria-label={t('回到底部')}
            title={t('回到底部')}
            initial={SCROLL_BTN_INITIAL}
            animate={SCROLL_BTN_ANIMATE}
            exit={SCROLL_BTN_EXIT}
            transition={SCROLL_BTN_TRANSITION}
          >
            <img src="/home/arrow-down.svg" alt="" className="jx-scrollToBottomIcon" />
          </motion.button>
        )}
      </AnimatePresence>
      <div className="jx-chatFooter">
        {!planMode && <PlanProgressStrip chatId={currentChatId} />}
        {/* 后台作业状态条：工作流模式下作业跑在后台，没有它就完全看不出在不在跑 */}
        <JobProgressStrip chatId={currentChatId} />
        <FileConfirmBar />
        <DesignPickerCard />
        {shareAccessLevel === 'read' ? (
          <div className="jx-chatShareReadonly">
            {t('该会话由创建者设为只读共享，无法在此发送消息')}
          </div>
        ) : hasPendingUserQuestion ? (
          <AskUserQuestionComposer />
        ) : (
          <InputArea
            inputRef={inputRef}
            fileInputRef={fileInputRef}
            send={() => send()}
            abort={abort}
            activateQueuedMessage={activateQueuedMessage}
            discardQueuedMessage={discardQueuedMessage}
            continueLoop={continueLoop}
            handleFileSelect={handleFileSelect}
            removeFile={removeFile}
            placeholder={inputPlaceholder}
            disableMention={isAgentChat}
          />
        )}
      </div>
    </div>
  );
}
