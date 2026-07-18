import {
  Button, Input,
} from 'antd';
import type { TextAreaRef } from 'antd/es/input/TextArea';
import React, { useCallback, useEffect, useLayoutEffect, useRef, useState, type MouseEvent } from 'react';
import { createPortal } from 'react-dom';
import { motion } from 'motion/react';

import {
  BulbOutlined, BulbFilled,
  DownOutlined,
  CopyOutlined, CheckOutlined,
  LikeOutlined, DislikeOutlined, LikeFilled, DislikeFilled,
  ExportOutlined, ShareAltOutlined, RedoOutlined,
  EditOutlined, SyncOutlined,
} from '@ant-design/icons';
import { extractArtifactOutputs } from '../../utils/fileParser';
import { getContextualCitations, getCitationOutputSlice, resolveConversationCitations } from '../../utils/citations';
import { ToolRunShell } from '../tool/ToolRunShell';
import type { ShellStep } from '../tool/ToolRunShell';
import { ToolProgressInline } from '../tool/ToolProgressInline';
import { anyToolRunning } from '../tool/renderers/utils';
import { ThinkingInline } from './ThinkingInline';
import { StreamWaitIndicator } from './StreamWaitIndicator';
import { useStallDetector } from '../../hooks';
import { PlanCard } from './PlanCard';
import { ArtifactCardList } from './ArtifactCardList';
import { CitationMarkdownBlock } from '../citation';
import { FileAttachmentCard } from '../file';
import { useChatStore, useUIStore } from '../../stores';
import { authFetch } from '../../api';
import type { ChatMessage, CitationItem } from '../../types';
import { FRESH_ENTER_WINDOW_MS } from '../../utils/motionTokens';
import { t } from '../../i18n';

const effectiveApiUrl = (import.meta.env.VITE_API_BASE_URL as string || '').trim() || '/api';

/** Format a message timestamp as local "YYYY-MM-DD HH:MM:SS", shown below each message. */
function formatMsgTime(ts: number): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Format the total generation time (milliseconds) as "用时 X.Xs"; when over 1 minute, use "用时 N分M秒". */
function formatDuration(ms?: number): string | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 60_000) return t('用时 {sec}s', { sec: (ms / 1000).toFixed(1) });
  const min = Math.floor(ms / 60_000);
  const sec = Math.round((ms % 60_000) / 1000);
  return t('用时 {min}分{sec}秒', { min, sec });
}

/** Wait for the jx-expandWrap height animation to finish expanding before focusing the input */
const EXPAND_FOCUS_DELAY_MS = 250;

/**
 * Lazy mount + keep on exit (lazy-keep-mounted): only mount content on first open (to avoid N messages
 * permanently rendering hidden antd TextAreas), then keep it mounted so the collapse animation can play.
 * On first open, render one frame in the closed state, then add the --open class after a double rAF, preserving the expand animation.
 */
function useLazyExpand(open: boolean) {
  const [mounted, setMounted] = useState(open);
  const [openClass, setOpenClass] = useState(false);
  useEffect(() => {
    if (!open) {
      setOpenClass(false);
      return;
    }
    if (mounted) {
      setOpenClass(true);
      return;
    }
    setMounted(true);
    let raf2 = 0;
    const raf1 = requestAnimationFrame(() => {
      raf2 = requestAnimationFrame(() => setOpenClass(true));
    });
    return () => {
      cancelAnimationFrame(raf1);
      cancelAnimationFrame(raf2);
    };
    // mounted is deliberately not in deps: to avoid the cleanup on first open canceling the not-yet-fulfilled double rAF
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  return { mounted, openClass };
}

/** Delayed focus after expanding (paired with useLazyExpand, focus only after the height animation completes) */
function useExpandFocus(open: boolean, ref: React.RefObject<TextAreaRef | null>) {
  useEffect(() => {
    if (!open) return;
    const id = window.setTimeout(() => ref.current?.focus({ cursor: 'end' }), EXPAND_FOCUS_DELAY_MS);
    return () => window.clearTimeout(id);
  }, [open, ref]);
}

interface MessageBubbleProps {
  m: ChatMessage;
  messageIndex: number;
  currentChatId: string;
  send: (text?: string) => void;
  exportChatRecord: (id: string) => Promise<void>;
  regenerate?: (messageIndex: number) => void;
  editAndResend?: (messageIndex: number, newContent: string) => void;
}

export function MessageBubble({ m, messageIndex, currentChatId, send, exportChatRecord, regenerate, editAndResend }: MessageBubbleProps) {
  const contentRef = useRef<HTMLDivElement | null>(null);
  const selectionRangeRef = useRef<Range | null>(null);
  const selectionCopiedTimerRef = useRef<number | null>(null);
  const selectionCopiedTextRef = useRef<string | null>(null);
  const selectionPointerDownRef = useRef<{ x: number; y: number; hadSelection: boolean } | null>(null);
  const selectionGuardUntilRef = useRef(0);
  const selectionToolbarRef = useRef<{ x: number; y: number; text: string } | null>(null);
  const [selectionToolbar, setSelectionToolbar] = useState<{ x: number; y: number; text: string } | null>(null);
  const [selectionCopied, setSelectionCopied] = useState(false);
  const {
    expandedThinking, toggleThinking,
    chatMode,
    copiedMsg, setCopiedMsg,
    feedbackMap, setFeedbackMap,
    dislikingTs, setDislikingTs,
    dislikeComment, setDislikeComment,
    shareSelectionMode, selectedShareMessageTs,
    toggleShareMessageTs, startShareSelectionWithAll,
    setQuotedFollowUp,
  } = useChatStore();
  const { setDetailModal, dispatchProcessVisible } = useUIStore();
  const chatMessages = useChatStore(state => state.store.chats[currentChatId]?.messages ?? []);
  const { editingMessageTs, setEditingMessageTs } = useChatStore();
  const [editText, setEditText] = useState('');
  const shareSelected = selectedShareMessageTs.has(m.ts);
  const isEditing = editingMessageTs === m.ts;
  const isDisliking = dislikingTs === m.ts;
  // Enter-animation gating: only "newly appended" messages play the enter animation (decided once at mount time and fixed),
  // old messages loaded from history / flushed in on session switch don't play — Bug B2 fix.
  const [isFresh] = useState(() => Date.now() - m.ts < FRESH_ENTER_WINDOW_MS);
  const editInputRef = useRef<TextAreaRef>(null);
  const dislikeInputRef = useRef<TextAreaRef>(null);

  // Edit box / dislike form: lazy-keep-mounted (only mount on first open + jx-expandWrap class-toggle for the height animation),
  // autoFocus is not triggered by mounting — focus is delayed until after expanding.
  const editExpand = useLazyExpand(isEditing);
  const dislikeExpand = useLazyExpand(isDisliking);
  useExpandFocus(isEditing, editInputRef);
  useExpandFocus(isDisliking, dislikeInputRef);
  const messagePlainText = m.segments
    ? m.segments.filter(s => s.type === 'text').map(s => s.content || '').join('\n\n') || m.content
    : m.content;

  // Drives the "正在准备调用工具" pending step inside the ToolRunShell — the
  // configured LLM buffers tool-call args server-side, so when the message is
  // streaming and goes silent (or backend has fired `tool_pending`) we want
  // *some* signal that work is still happening. Replaces the old free-floating
  // StreamWaitIndicator below the text bubble.
  const stallSignature = `${(m.content || '').length}|${m.toolCalls?.length ?? 0}|${m.segments?.length ?? 0}`;
  // Anchor the stall clock to the message's persisted `lastActivityTs` so the
  // "正在准备调用工具…" timer keeps counting from the real start even after a
  // session switch or page refresh remounts this component.
  const stall = useStallDetector(stallSignature, 2500, m.lastActivityTs);
  const noToolRunning = !anyToolRunning(m.toolCalls || []);
  const pendingWaiting = !!m.isStreaming && noToolRunning && (!!m.toolPending || stall.waiting);

  const hideSelectionToolbar = () => {
    selectionToolbarRef.current = null;
    selectionCopiedTextRef.current = null;
    setSelectionToolbar(null);
    setSelectionCopied(false);
  };

  const guardSelectionState = () => {
    selectionGuardUntilRef.current = Date.now() + 80;
  };

  const restoreSelectionRange = () => {
    if (!selectionRangeRef.current) return;
    const selection = window.getSelection();
    if (!selection) return;
    selection.removeAllRanges();
    selection.addRange(selectionRangeRef.current);
  };

  const hasSelectionInsideContent = () => {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed || !contentRef.current) {
      return false;
    }
    const range = selection.getRangeAt(0);
    return contentRef.current.contains(range.startContainer) || contentRef.current.contains(range.endContainer);
  };

  const clearSelectionState = () => {
    selectionGuardUntilRef.current = 0;
    selectionRangeRef.current = null;
    const selection = window.getSelection();
    if (selection) {
      selection.removeAllRanges();
    }
    hideSelectionToolbar();
  };

  const getStoredSelectionText = () => {
    const current = window.getSelection()?.toString().trim();
    if (current) return current;
    const stored = selectionRangeRef.current?.toString().trim();
    return stored || '';
  };

  /**
   * Check if the current window selection overlaps with this message's
   * contentRef and, if so, show the toolbar.  Called from both mouseup
   * and selectionchange so we catch every path (mouse, keyboard, touch).
   */
  const checkAndShowToolbar = (fallbackRange?: Range | null) => {
    const selection = window.getSelection();
    let range: Range | null = null;
    let selectedText = '';

    if (selection && selection.rangeCount > 0 && !selection.isCollapsed) {
      range = selection.getRangeAt(0).cloneRange();
      selectedText = selection.toString().trim();
    } else if (fallbackRange) {
      range = fallbackRange.cloneRange();
      selectedText = range.toString().trim();
    }

    if (!range || !selectedText) {
      hideSelectionToolbar();
      selectionRangeRef.current = null;
      return;
    }

    // At least one endpoint of the selection must be inside our content
    const anchorNode = selection && !selection.isCollapsed ? selection.anchorNode : range.startContainer;
    const focusNode = selection && !selection.isCollapsed ? selection.focusNode : range.endContainer;
    if (!contentRef.current || !anchorNode || !focusNode) {
      hideSelectionToolbar();
      selectionRangeRef.current = null;
      return;
    }

    const anchorInside = contentRef.current.contains(anchorNode);
    const focusInside = contentRef.current.contains(focusNode);
    if (!anchorInside && !focusInside) {
      // Selection is entirely outside this bubble — ignore
      hideSelectionToolbar();
      selectionRangeRef.current = null;
      return;
    }

    selectionRangeRef.current = range.cloneRange();
    const rect = range.getBoundingClientRect();
    if (!rect.width && !rect.height) {
      hideSelectionToolbar();
      selectionRangeRef.current = null;
      return;
    }

    const pos = { x: rect.left + rect.width / 2, y: rect.top - 12, text: selectedText };
    selectionToolbarRef.current = pos;
    guardSelectionState();
    if (selectionCopiedTextRef.current !== selectedText) {
      setSelectionCopied(false);
    }
    setSelectionToolbar(pos);
  };

  const handleSelectionFollowUpQuote = () => {
    const quoteText = getStoredSelectionText() || selectionToolbar?.text || '';
    if (!quoteText) return;
    setQuotedFollowUp({ text: quoteText, ts: m.ts });
    restoreSelectionRange();
  };

  const handleSelectionToolbarMouseDown = (
    e: MouseEvent<HTMLButtonElement>,
    action: () => void,
  ) => {
    e.preventDefault();
    e.stopPropagation();
    guardSelectionState();
    restoreSelectionRange();
    action();
  };

  // Defensive: restore selection after React re-render caused by toolbar state update.
  // When setSelectionToolbar(pos) triggers a re-render, the DOM reconciliation may
  // cause the browser to lose the active selection. This effect restores it.
  useLayoutEffect(() => {
    if (selectionToolbar && selectionRangeRef.current) {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) {
        guardSelectionState();
        restoreSelectionRange();
      }
    }
  }, [selectionToolbar]);

  // ---------- Selection event listeners ----------
  // Registered ONCE ([] deps) so listeners are never torn down and re-added.
  // This avoids the race where React's async effect cleanup removes the
  // selectionchange listener while a pending selectionchange event fires
  // and clears the selection.
  useEffect(() => {
    const handleSelectionChange = () => {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed) {
        if (Date.now() < selectionGuardUntilRef.current) {
          return;
        }
        // Only update state if toolbar is currently shown (avoid unnecessary re-renders)
        if (selectionToolbarRef.current) {
          selectionToolbarRef.current = null;
          selectionRangeRef.current = null;
          setSelectionToolbar(null);
          setSelectionCopied(false);
        }
      }
    };

    const handleWindowScroll = () => {
      // Read ref (always current) instead of closed-over state
      if (selectionToolbarRef.current && selectionRangeRef.current) {
        checkAndShowToolbar(selectionRangeRef.current);
      }
    };

    document.addEventListener('selectionchange', handleSelectionChange);
    window.addEventListener('scroll', handleWindowScroll, true);
    window.addEventListener('resize', handleWindowScroll);

    return () => {
      if (selectionCopiedTimerRef.current) {
        window.clearTimeout(selectionCopiedTimerRef.current);
      }
      document.removeEventListener('selectionchange', handleSelectionChange);
      window.removeEventListener('scroll', handleWindowScroll, true);
      window.removeEventListener('resize', handleWindowScroll);
    };
  }, []);

  /** Render a thinking block */
  const renderThinkingBlock = (content: string, thinkKey: string, isActiveThinking: boolean) => {
    const isExpanded = isActiveThinking || expandedThinking.has(thinkKey);
    const toggleThink = () => {
      if (isActiveThinking) return;
      toggleThinking(thinkKey);
    };
    return (
      <div key={thinkKey} className="jx-thinkingBlock">
        <div className={`jx-thinkingBlockHeader${isActiveThinking ? ' jx-thinkingActive' : ''}`}
          role="button" tabIndex={0} onClick={toggleThink}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleThink(); } }}>
          <div className="jx-thinkingHeaderLeft">
            {isActiveThinking ? <div className="jx-thinkingSpinner" /> : <BulbFilled className="jx-thinkingIcon" />}
            <span className="jx-thinkingLabel">{isActiveThinking ? t('正在思考…') : t('思考过程')}</span>
          </div>
          {!isActiveThinking && (
            <DownOutlined className={`jx-expandIcon${isExpanded ? ' jx-expandIcon--open' : ''}`} />
          )}
        </div>
        <div className={`jx-expandWrap${isExpanded ? ' jx-expandWrap--open' : ''}`}>
          <div className="jx-thinkingContent" ref={(el) => { if (el && isActiveThinking) el.scrollTop = el.scrollHeight; }}>
            {(isExpanded || isActiveThinking) && content}
          </div>
        </div>
      </div>
    );
  };

  /** Render file download/image artifact cards */
  const renderArtifactCards = () => {
    if (!m.toolCalls || m.isStreaming) return null;
    const artifactMap = new Map<string, any>();
    const pushArtifact = (artifact: any) => {
      if (!artifact?.file_id) return;
      artifactMap.set(String(artifact.file_id), artifact);
    };
    for (const tool of m.toolCalls) {
      const out = tool.output as any;
      if (tool.status !== 'success' && tool.status != null) {
        continue;
      }
      // pin_to_workspace returns a {file_id, name, ...} shape that would
      // otherwise render as a duplicate card. The workspace_files allowlist
      // (handled below) already covers what should be shown, so suppress
      // the pin tool's own output from contributing artifact entries.
      if (tool.name !== 'pin_to_workspace') {
        for (const artifact of extractArtifactOutputs(out)) {
          pushArtifact(artifact);
        }
      }
    }
    // Strict workspace gate: pin_to_workspace is the only way for a file
    // to surface in the conversation. workspaceFiles is an array (possibly
    // empty) on every new message — empty means the agent didn't pin
    // anything, so nothing renders. When the field is missing entirely
    // (undefined) the message predates this feature → legacy fallback
    // (show every artifact extracted from tool outputs).
    let artifacts = Array.from(artifactMap.values());
    if (Array.isArray(m.workspaceFiles)) {
      const allow = new Set(m.workspaceFiles);
      artifacts = artifacts.filter((a) => allow.has(String(a.file_id)));
    }
    if (artifacts.length === 0) return null;
    return <ArtifactCardList artifacts={artifacts} />;
  };

  /** Open citation detail */
  const openCitationAction = (citation: CitationItem, toolCalls?: ChatMessage['toolCalls']) => {
    const { toolName, output } = getCitationOutputSlice(citation, toolCalls);

    if (toolName === 'internet_search') {
      const data = (typeof output === 'object' && output !== null ? output : {}) as any;
      const searchResult = data?.result ?? data;
      const results: any[] = Array.isArray(searchResult?.results) ? searchResult.results : [];
      const first = results[0] ?? {};
      const targetUrl = String(first?.url || citation.url || '');
      if (targetUrl) {
        window.open(targetUrl, '_blank', 'noopener,noreferrer');
        return;
      }
      const title = String(first?.title || citation.title || t('互联网搜索结果'));
      const content = String(first?.content || first?.snippet || citation.snippet || t('暂无内容'));
      setDetailModal({ title, body: <div className="jx-tr-detailBody">{content}</div> });
      return;
    }

    if (toolName === 'retrieve_dataset_content') {
      const data = (typeof output === 'object' && output !== null ? output : {}) as any;
      const item = Array.isArray(data?.items) ? data.items[0] : undefined;
      const docName = String(item?.['文件名称'] || item?.title || item?.document_name || citation.title || t('未知文档'));
      const content = String(item?.['文件内容'] || item?.content || citation.snippet || '');
      setDetailModal({ title: docName, body: <div className="jx-tr-detailBody">{content || t('暂无内容')}</div> });
      return;
    }

    if (toolName === 'get_industry_news') {
      const data = (typeof output === 'object' && output !== null ? output : {}) as any;
      const item = Array.isArray(data?.items) ? data.items[0] : undefined;
      const title = String(item?.['标题'] || item?.title || citation.title || t('资讯详情'));
      const summary = String(item?.['摘要'] || item?.summary || citation.snippet || '');
      const tags = [item?.['标签'], item?.['对应产业链'], item?.['地区']].filter(Boolean).map(String);
      setDetailModal({
        title,
        body: (
          <div>
            {tags.length > 0 && <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 10 }}>{tags.map((tag, ti) => <span key={ti} className="jx-tr-newsTag">{tag}</span>)}</div>}
            <div className="jx-tr-detailBody">{summary || t('暂无摘要')}</div>
          </div>
        ),
      });
      return;
    }

    // fallback
    const title = citation.title || t('引用详情');
    const snippet = citation.snippet || t('暂无内容');
    setDetailModal({ title, body: <div className="jx-tr-detailBody">{snippet}</div> });
  };

  // Stable callback for citation actions — prevents CitationMarkdownBlock re-renders
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const handleCitationAction = useCallback(
    (citation: CitationItem) => openCitationAction(citation, m.toolCalls),
    [m.toolCalls],
  );

  /** Copy message text */
  const doCopy = (str: string) => {
    const copyFallback = (s: string) => {
      const ta = document.createElement('textarea');
      ta.value = s; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); document.body.removeChild(ta);
      setCopiedMsg(m.ts);
      setTimeout(() => { if (useChatStore.getState().copiedMsg === m.ts) setCopiedMsg(null); }, 2000);
    };
    if (navigator.clipboard) {
      navigator.clipboard.writeText(str).then(() => {
        setCopiedMsg(m.ts);
        setTimeout(() => { if (useChatStore.getState().copiedMsg === m.ts) setCopiedMsg(null); }, 2000);
      }).catch(() => copyFallback(str));
    } else {
      copyFallback(str);
    }
  };

  const doSelectionCopy = (raw: string) => {
    const str = getStoredSelectionText() || raw;
    if (!str) return;
    const markSelectionCopied = () => {
      selectionCopiedTextRef.current = str;
      setSelectionCopied(true);
      if (selectionCopiedTimerRef.current) {
        window.clearTimeout(selectionCopiedTimerRef.current);
      }
      selectionCopiedTimerRef.current = window.setTimeout(() => {
        selectionCopiedTextRef.current = null;
        setSelectionCopied(false);
        selectionCopiedTimerRef.current = null;
      }, 1800);
    };

    const copyFallback = (s: string) => {
      const ta = document.createElement('textarea');
      ta.value = s;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      restoreSelectionRange();
      markSelectionCopied();
    };

    if (navigator.clipboard) {
      navigator.clipboard.writeText(str).then(() => {
        restoreSelectionRange();
        markSelectionCopied();
      }).catch(() => copyFallback(str));
    } else {
      copyFallback(str);
    }
  };

  const sending = useChatStore.getState().sending;
  const renderUserQuote = () => {
    if (m.role !== 'user' || !m.quotedFollowUp?.text) return null;
    return (
      <div className="jx-userQuote" title={m.quotedFollowUp.text}>
        <span className="jx-userQuoteLabel">{t('引用')}</span>
        <span className="jx-userQuoteText">{m.quotedFollowUp.text}</span>
      </div>
    );
  };

  const renderChipBadges = () => {
    if (m.role !== 'user') return null;
    const hasMention = !!m.mentionName;
    const hasSkill = !!m.skillName;
    const hasPlugin = !!m.pluginName;
    if (!hasMention && !hasSkill && !hasPlugin) return null;
    return (
      <div className="jx-msgChipBadges">
        {hasMention && (
          <span className="jx-msgChip jx-msgChip--mention">
            <span className="jx-msgChip-prefix">@</span>{m.mentionName}
          </span>
        )}
        {hasSkill && (
          <span className="jx-msgChip jx-msgChip--skill">
            <span className="jx-msgChip-prefix">/</span>{m.skillName}
          </span>
        )}
        {hasPlugin && (
          <span className="jx-msgChip jx-msgChip--plugin">
            <span className="jx-msgChip-prefix">/</span>{m.pluginName}
          </span>
        )}
      </div>
    );
  };

  return (
    <div
      className={`jx-msg ${m.role === 'user' ? 'user' : 'assistant'}${isFresh ? ' jx-msg--fresh' : ''}`}
      data-message-ts={m.ts}
    >
      <div className={`jx-msgInner${m.role === 'user' ? ' user' : ''}${shareSelectionMode && !m.isStreaming ? ' share-selectable' : ''}`}>
        {shareSelectionMode && !m.isStreaming && (
          <label className="jx-shareCheckboxWrap" aria-label={t('选择这条对话记录')}>
            <input
              type="checkbox"
              className="jx-shareCheckbox"
              checked={shareSelected}
              onChange={() => toggleShareMessageTs(m.ts)}
            />
          </label>
        )}
      <div
        className="jx-msgContent"
        ref={contentRef}
        onMouseDown={(e) => {
          if (e.button !== 0) return;
          selectionPointerDownRef.current = {
            x: e.clientX,
            y: e.clientY,
            hadSelection: hasSelectionInsideContent(),
          };
        }}
        onMouseUp={(e) => {
          const pointerDown = selectionPointerDownRef.current;
          selectionPointerDownRef.current = null;
          if (pointerDown?.hadSelection) {
            const moved = Math.abs(e.clientX - pointerDown.x) > 3 || Math.abs(e.clientY - pointerDown.y) > 3;
            if (!moved) {
              clearSelectionState();
              return;
            }
          }

          // Save selection range immediately before any async processing,
          // so it can be restored if a React re-render destroys the selection.
          const sel = window.getSelection();
          let capturedRange: Range | null = null;
          if (sel && sel.rangeCount > 0 && !sel.isCollapsed) {
            capturedRange = sel.getRangeAt(0).cloneRange();
            selectionRangeRef.current = capturedRange;
            guardSelectionState();
          }
          window.setTimeout(() => checkAndShowToolbar(capturedRange), 0);
        }}
      >
        {/* User attachments */}
        {m.role === 'user' && m.attachments && m.attachments.length > 0 && (
          <div className="jx-userAttachments">
            {m.attachments.map((att, idx) => (
              <FileAttachmentCard key={idx} name={att.name} downloadHref={(att.download_url || att.file_id) ? `${effectiveApiUrl}${att.download_url || `/files/${att.file_id}`}` : undefined} />
            ))}
          </div>
        )}

        {m.segments && m.segments.length > 0 ? (
          /* Segment-based rendering */
          <>
            {(() => {
              // Group thinking + tool calls + tool-prepare waits into a single
              // "agent run" shell so the chat flow shows one card per phase
              // instead of separate thinking / tool-call / preparing entries.
              //
              // A run is a maximal contiguous sequence of {thinking, tool,
              // empty-text} segments. Non-empty text breaks the run. Pure
              // empty-text only chunks are absorbed but never anchor a run.
              //
              // OFF mode (dispatchProcessVisible=false) keeps the existing
              // ToolProgressInline for tool batches; pending state still
              // surfaces, but the unified shell only takes over in ON mode.
              const segs = m.segments!;
              const isEmptyText = (i: number): boolean => {
                const s = segs[i];
                return !!s && s.type === 'text' && !(s.content || '').trim();
              };
              const hasNonEmptyTextAfter = (idx: number): boolean => (
                segs.slice(idx + 1).some(s => s.type === 'text' && !!(s.content || '').trim())
              );

              type Run = {
                anchor: number;
                endIdx: number;
                steps: ShellStep[];
                tools: NonNullable<typeof m.toolCalls>;
              };
              const runs: Run[] = [];
              const suppressedIdx = new Set<number>();

              const canStartRun = (idx: number): boolean => {
                const s = segs[idx];
                if (!s) return false;
                if (s.type === 'tool') return true;
                // Only ON mode lets a thinking segment anchor a run; OFF mode
                // keeps standalone ThinkingInline as before.
                if (dispatchProcessVisible && s.type === 'thinking') return true;
                return false;
              };

              let i = 0;
              while (i < segs.length) {
                if (!canStartRun(i)) {
                  i++;
                  continue;
                }
                const anchor = i;
                const steps: ShellStep[] = [];
                const tools: NonNullable<typeof m.toolCalls> = [] as NonNullable<typeof m.toolCalls>;
                let endIdx = -1;
                while (i < segs.length) {
                  const sk = segs[i];
                  if (sk.type === 'tool') {
                    const t = m.toolCalls?.[sk.toolIndex!];
                    if (t) {
                      steps.push({ kind: 'tool', tool: t, key: `${m.ts}-seg-${i}` });
                      tools.push(t);
                    }
                    if (i !== anchor) suppressedIdx.add(i);
                    endIdx = i;
                    i++;
                  } else if (sk.type === 'thinking') {
                    if (dispatchProcessVisible) {
                      const content = sk.content || '';
                      const active = !!(m.isStreaming && !segs.slice(i + 1).some(seg => seg.type === 'text'));
                      steps.push({ kind: 'thinking', content, active, key: `${m.ts}-seg-${i}` });
                      endIdx = i;
                    }
                    if (i !== anchor) suppressedIdx.add(i);
                    i++;
                  } else if (isEmptyText(i)) {
                    if (i !== anchor) suppressedIdx.add(i);
                    i++;
                  } else {
                    break;
                  }
                }
                // OFF mode un-suppresses trailing empty-text so the inline
                // StreamWaitIndicator can host there. ON mode keeps them
                // suppressed — the shell owns the wait state and an empty
                // bubble below it would just add visual noise.
                if (endIdx >= 0 && !dispatchProcessVisible) {
                  for (let j = endIdx + 1; j < i; j++) {
                    if (isEmptyText(j)) suppressedIdx.delete(j);
                  }
                }
                if (steps.length > 0) {
                  runs.push({ anchor, endIdx, steps, tools });
                } else {
                  // Run with no real steps (only empty text) — un-suppress
                  // everything so we don't accidentally swallow useful state.
                  for (let j = anchor; j < i; j++) suppressedIdx.delete(j);
                }
              }

              // ON mode owns the wait state inside the shell, so any stray
              // empty-text segments outside a run would just paint an empty
              // bubble. Suppress them.
              if (dispatchProcessVisible) {
                for (let j = 0; j < segs.length; j++) {
                  if (isEmptyText(j)) suppressedIdx.add(j);
                }
              }

              // Attach a pending step where the wait state should appear.
              // - In ON mode + an active run that runs to the end of segments
              //   (no text after) → append to that run's steps.
              // - In ON mode otherwise (no run yet, or finished with text) →
              //   render a separate "virtual" mini-shell below the last
              //   segment so the user sees *some* progress signal.
              // - In OFF mode → fall through to the per-text-segment
              //   StreamWaitIndicator (preserves existing inline behavior).
              const hasVisibleTextProgress = segs.some(s => s.type === 'text' && (s.content || '').trim().length > 0);
              const startupPending = !!m.isStreaming && dispatchProcessVisible && runs.length === 0 && !hasVisibleTextProgress;
              const showPendingInShell = pendingWaiting && dispatchProcessVisible;
              let virtualPending: { startTs: number; key: string } | null = null;
              if (showPendingInShell || startupPending) {
                const lastRun = runs.length ? runs[runs.length - 1] : null;
                const lastRunReachesEnd = !!lastRun && (() => {
                  for (let j = lastRun.endIdx + 1; j < segs.length; j++) {
                    if (!isEmptyText(j)) return false;
                  }
                  return true;
                })();
                if (lastRun && lastRunReachesEnd) {
                  lastRun.steps.push({ kind: 'pending', startTs: stall.since, key: `${m.ts}-pending` });
                } else {
                  virtualPending = { startTs: stall.since, key: `${m.ts}-pending-virtual` };
                }
              }

              const runByAnchor = new Map<number, Run>();
              runs.forEach((r) => runByAnchor.set(r.anchor, r));

              const rendered = segs.map((seg, segIdx) => {
                const isLastSeg = segIdx === segs.length - 1;
                const segKey = `${m.ts}-seg-${segIdx}`;

                if (runByAnchor.has(segIdx)) {
                  const run = runByAnchor.get(segIdx)!;
                  if (dispatchProcessVisible) {
                    return (
                      <ToolRunShell
                        key={segKey}
                        steps={run.steps}
                        isStreaming={m.isStreaming}
                        holdOpenUntilText={!!m.isStreaming && !hasNonEmptyTextAfter(run.endIdx)}
                      />
                    );
                  }
                  if (run.tools.length > 0) {
                    return <ToolProgressInline key={segKey} message={m} toolCalls={run.tools} />;
                  }
                  return null;
                }

                if (suppressedIdx.has(segIdx)) return null;

                if (seg.type === 'tool') {
                  return null;
                }

                if (seg.type === 'plan' && seg.planData) {
                  return (
                    <PlanCard
                      key={segKey}
                      mode={seg.planData.mode}
                      title={seg.planData.title}
                      description={seg.planData.description}
                      steps={seg.planData.steps}
                      completedSteps={seg.planData.completedSteps}
                      totalSteps={seg.planData.totalSteps}
                      resultText={seg.planData.resultText}
                      isStreaming={m.isStreaming}
                      agentNameMap={seg.planData.agentNameMap}
                    />
                  );
                }

                if (seg.type === 'thinking') {
                  // OFF mode: thinking that wasn't folded into a tool run still
                  // renders as its inline summary.
                  const isActiveThinking = !!(m.isStreaming && !m.segments!.slice(segIdx + 1).some(s => s.type === 'text'));
                  return <ThinkingInline key={segKey} content={seg.content || ''} thinkKey={segKey} isActive={isActiveThinking} />;
                }

                if (seg.type === 'text') {
                  const textContent = seg.content || '';
                  if (!textContent && !m.isStreaming) return null;
                  const msgCitations = getContextualCitations(m.citations ?? [], m.segments, m.toolCalls, segIdx);
                  const effectiveCitations = resolveConversationCitations(textContent, msgCitations, chatMessages, m.ts);
                  // OFF mode keeps the inline StreamWaitIndicator under the
                  // text bubble; ON mode handles waits inside the shell so
                  // we suppress the indicator entirely.
                  const showInlineWait = !dispatchProcessVisible && m.isStreaming && isLastSeg;
                  return (
                    <React.Fragment key={segKey}>
                      <div className={`jx-bubble ${m.role === 'user' ? 'user' : ''} ${m.isMarkdown ? 'jx-md' : ''} ${m.isStreaming && isLastSeg ? 'streaming' : ''}`}>
                        {segIdx === 0 && renderUserQuote()}
                        {segIdx === 0 && renderChipBadges()}
                        <CitationMarkdownBlock
                          className="jx-msgText"
                          text={textContent}
                          isMarkdown={m.isMarkdown ?? false}
                          citations={effectiveCitations}
                          messageIsStreaming={m.isStreaming}
                          onCitationAction={handleCitationAction}
                        />
                      </div>
                      {showInlineWait && (
                        <StreamWaitIndicator
                          signature={stallSignature}
                          forceWait={!!m.toolPending}
                          suppressed={anyToolRunning(m.toolCalls || [])}
                          anchorTs={m.lastActivityTs}
                        />
                      )}
                    </React.Fragment>
                  );
                }
                return null;
              });

              if (virtualPending) {
                rendered.push(
                  <ToolRunShell
                    key={virtualPending.key}
                    steps={[{ kind: 'pending', startTs: virtualPending.startTs, key: virtualPending.key }]}
                    isStreaming={m.isStreaming}
                    holdOpenUntilText={true}
                  />,
                );
              }

              return rendered;
            })()}
          </>
        ) : (
          /* Legacy rendering path */
          <>
            {m.toolCalls && m.toolCalls.length > 0 && dispatchProcessVisible && (
              <div className="jx-toolCallsList">
                <ToolRunShell
                  steps={m.toolCalls.map((tool, idx) => ({ kind: 'tool' as const, tool, key: `${m.ts}-legacy-${idx}` }))}
                  isStreaming={m.isStreaming}
                  holdOpenUntilText={!!m.isStreaming && !(m.content || '').trim()}
                />
              </div>
            )}
            {m.thinking && m.thinking.length > 0 && chatMode !== 'fast' && (
              <div className="jx-thinkingSection">
                <div className="jx-sectionHeader">
                  <BulbOutlined className="jx-sectionIcon" />
                  <span className="jx-sectionTitle">{t('思考过程 ({n})', { n: m.thinking.length })}</span>
                </div>
                <div className="jx-thinkingList">
                  {m.thinking.map((think, idx) => renderThinkingBlock(think.content, `${m.ts}-think-${idx}`, false))}
                </div>
              </div>
            )}
            {m.isStreaming && !m.content ? (
              dispatchProcessVisible ? (
                <ToolRunShell
                  steps={[{ kind: 'pending', startTs: stall.since, key: `${m.ts}-legacy-pending` }]}
                  isStreaming={m.isStreaming}
                  holdOpenUntilText={true}
                />
              ) : (
                <ThinkingInline content="" thinkKey={`${m.ts}-legacy-placeholder`} isActive={true} />
              )
            ) : (
            <div className={`jx-bubble ${m.role === 'user' ? 'user' : ''} ${m.isMarkdown ? 'jx-md' : ''} ${m.isStreaming ? 'streaming' : ''}`}>
              {renderUserQuote()}
              {renderChipBadges()}
              <CitationMarkdownBlock
                className="jx-msgText"
                text={m.content}
                isMarkdown={m.isMarkdown ?? false}
                citations={resolveConversationCitations(m.content, m.citations ?? [], chatMessages, m.ts)}
                messageIsStreaming={m.isStreaming}
                onCitationAction={handleCitationAction}
              />
              {m.isStreaming && (
                <span className="jx-streamingIndicator" aria-hidden="true">
                  <span className="jx-streamingDot" /><span className="jx-streamingDot" /><span className="jx-streamingDot" />
                </span>
              )}
            </div>
            )}
          </>
        )}

        {/* Artifact cards */}
        {m.role === 'assistant' && renderArtifactCards()}

        {/* Follow-up questions */}
        {m.role === 'assistant' && m.followUpQuestions && m.followUpQuestions.length > 0 && (
          <motion.div
            className="jx-followUpQuestions"
            initial="hidden"
            animate="visible"
            variants={{ visible: { transition: { staggerChildren: 0.06, delayChildren: 0.05 } } }}
          >
            {m.followUpQuestions.map((q, qi) => (
              <motion.button
                key={qi}
                className="jx-followUpBtn"
                variants={{
                  hidden: { opacity: 0, y: 6 },
                  visible: { opacity: 1, y: 0, transition: { duration: 0.2, ease: 'easeOut' } },
                }}
                onClick={() => send(q)}
                disabled={sending}
              >
                <span className="jx-followUpText">{q}</span>
                <span className="jx-followUpArrow">→</span>
              </motion.button>
            ))}
          </motion.div>
        )}

        {/* Selection quick menu — rendered via portal to document.body so that
            position:fixed is relative to the viewport, not to any transformed
            Framer-Motion ancestor which would otherwise break fixed positioning. */}
        {!m.isStreaming && selectionToolbar && createPortal(
          <div
            className="jx-selectionToolbar"
            style={{ left: selectionToolbar.x, top: selectionToolbar.y }}
          >
            <button
              type="button"
              className={`jx-selectionToolbarBtn${selectionCopied ? ' copied' : ''}`}
              title={selectionCopied ? t('已复制') : t('复制')}
              onMouseDown={(e) => handleSelectionToolbarMouseDown(e, () => {
                doSelectionCopy(selectionToolbar.text);
              })}
            >
              <span className="jx-selectionToolbarIcon">{selectionCopied ? <CheckOutlined /> : <CopyOutlined />}</span>
              <span className="jx-selectionToolbarLabel">{selectionCopied ? t('已复制') : t('复制')}</span>
            </button>
            <button
              type="button"
              className="jx-selectionToolbarBtn"
              title={t('追问')}
              onMouseDown={(e) => handleSelectionToolbarMouseDown(e, handleSelectionFollowUpQuote)}
            >
              <span className="jx-selectionToolbarIcon"><RedoOutlined /></span>
              <span className="jx-selectionToolbarLabel">{t('追问')}</span>
            </button>
          </div>,
          document.body,
        )}

        {/* User message editing — lazy-keep-mounted: only mount on first open, then keep it to play the collapse animation */}
        {m.role === 'user' && !!editAndResend && editExpand.mounted && (
          <div className={`jx-expandWrap jx-msgExpand${editExpand.openClass ? ' jx-expandWrap--open' : ''}`}>
            <div className="jx-editMessage">
              <Input.TextArea
                ref={editInputRef}
                rows={3}
                value={editText}
                onChange={e => setEditText(e.target.value)}
                className="jx-editMessage-input"
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    if (editText.trim() && editAndResend) {
                      editAndResend(messageIndex, editText.trim());
                    }
                  }
                }}
              />
              <div className="jx-editMessage-btns">
                <Button size="small" onClick={() => setEditingMessageTs(null)}>{t('取消')}</Button>
                <Button size="small" type="primary" disabled={!editText.trim()}
                  onClick={() => {
                    if (editAndResend) {
                      editAndResend(messageIndex, editText.trim());
                    }
                  }}>{t('发送')}</Button>
              </div>
            </div>
          </div>
        )}

        {/* Message action bar */}
        {!m.isStreaming && !isEditing && (
          <div className={`jx-msgActions ${m.role === 'user' ? 'user' : ''}`}>
            <button className={`jx-msgActionBtn${copiedMsg === m.ts ? ' copied' : ''}`}
              title={copiedMsg === m.ts ? t('已复制') : t('复制内容')}
              onClick={() => doCopy(messagePlainText)}>
              {copiedMsg === m.ts ? <CheckOutlined /> : <CopyOutlined />}
            </button>
            {m.role === 'user' && editAndResend && (
              <button className="jx-msgActionBtn" title={t('编辑消息')}
                onClick={() => {
                  setEditText(m.content);
                  setEditingMessageTs(m.ts);
                }}>
                <EditOutlined />
              </button>
            )}
            {m.role === 'assistant' && (<>
              <button className={`jx-msgActionBtn${feedbackMap[m.ts] === 'like' ? ' active-like' : ''}`} title={t('有帮助')}
                onClick={() => {
                  const next = feedbackMap[m.ts] === 'like' ? undefined : 'like' as const;
                  setFeedbackMap(next ? { ...feedbackMap, [m.ts]: next } : Object.fromEntries(Object.entries(feedbackMap).filter(([k]) => Number(k) !== m.ts)));
                  if (next && m.messageId) {
                    authFetch(`${effectiveApiUrl}/v1/chats/messages/${m.messageId}/feedback`, {
                      method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ rating: 'like', chat_id: currentChatId }),
                    }).catch(() => {});
                  }
                }}>
                {feedbackMap[m.ts] === 'like' ? <LikeFilled /> : <LikeOutlined />}
              </button>
              <button className={`jx-msgActionBtn${feedbackMap[m.ts] === 'dislike' ? ' active-dislike' : ''}`} title={t('没有帮助')}
                onClick={() => {
                  if (feedbackMap[m.ts] === 'dislike') {
                    setFeedbackMap(Object.fromEntries(Object.entries(feedbackMap).filter(([k]) => Number(k) !== m.ts)));
                    setDislikingTs(null);
                  } else {
                    setFeedbackMap({ ...feedbackMap, [m.ts]: 'dislike' });
                    setDislikingTs(m.ts);
                    setDislikeComment('');
                  }
                }}>
                {feedbackMap[m.ts] === 'dislike' ? <DislikeFilled /> : <DislikeOutlined />}
              </button>
            </>)}
            {m.role === 'assistant' && (
              <button className="jx-msgActionBtn" title={t('导出为PDF文件')} aria-label={t('导出为PDF文件')} onClick={() => { void exportChatRecord(currentChatId); }}>
                <ExportOutlined />
              </button>
            )}
            {m.role === 'assistant' && (
              <button
                className={`jx-msgActionBtn${shareSelectionMode ? ' active-share' : ''}`}
                title={t('生成分享链接')}
                aria-label={t('生成分享链接')}
                onClick={() => {
                  // By default select all completed messages in the current conversation, so clicking share can generate the link directly
                  startShareSelectionWithAll(
                    chatMessages.filter((msg) => !msg.isStreaming).map((msg) => msg.ts),
                  );
                }}
              >
                <ShareAltOutlined />
              </button>
            )}
            {m.role === 'assistant' && formatDuration(m.durationMs) && (
              <span className="jx-msgDuration" title={t('本次回答整体生成耗时')}>
                {formatDuration(m.durationMs)}
              </span>
            )}
            {m.role === 'assistant' && regenerate && (
              <button className="jx-msgActionBtn" title={t('重新生成')}
                onClick={() => regenerate(messageIndex)}>
                <SyncOutlined />
              </button>
            )}
            {m.role === 'user' && (
              <span className="jx-msgTime">{formatMsgTime(m.ts)}</span>
            )}
          </div>
        )}

        {/* Dislike feedback form — lazy-keep-mounted: only mount on first open, then keep it to play the collapse animation */}
        {m.role === 'assistant' && dislikeExpand.mounted && (
          <div className={`jx-expandWrap jx-msgExpand${dislikeExpand.openClass ? ' jx-expandWrap--open' : ''}`}>
            <div className="jx-dislikeFeedback">
              <p className="jx-dislikeFeedback-title">{t('请告诉我们哪里不好（可选）')}</p>
              <Input.TextArea ref={dislikeInputRef} rows={3} placeholder={t('内容不准确 / 答非所问 / 其他...')}
                value={dislikeComment} onChange={e => setDislikeComment(e.target.value)} className="jx-dislikeFeedback-input" />
              <div className="jx-dislikeFeedback-btns">
                <Button size="small" onClick={() => setDislikingTs(null)}>{t('跳过')}</Button>
                <Button size="small" type="primary" onClick={() => {
                  if (m.messageId) {
                    authFetch(`${effectiveApiUrl}/v1/chats/messages/${m.messageId}/feedback`, {
                      method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ rating: 'dislike', comment: dislikeComment || undefined, chat_id: currentChatId }),
                    }).catch(() => {});
                  }
                  setDislikingTs(null);
                }}>{t('提交')}</Button>
              </div>
            </div>
          </div>
        )}
      </div>
      </div>
    </div>
  );
}
