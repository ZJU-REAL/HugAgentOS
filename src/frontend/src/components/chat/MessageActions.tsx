import { Button, Input } from 'antd';
import type { TextAreaRef } from 'antd/es/input/TextArea';
import React, { useEffect, useRef, useState } from 'react';
import {
  CopyOutlined, CheckOutlined, LikeOutlined, DislikeOutlined, LikeFilled, DislikeFilled,
  ExportOutlined, ShareAltOutlined, EditOutlined, SyncOutlined,
} from '@ant-design/icons';
import { useChatStore } from '../../stores';
import { isAutomationHistoryChat } from '../../utils/history';
import { useAuthStore } from '../../stores/authStore';
import { useEvaluationStore } from '../../stores/evaluationStore';
import { EvaluationCard } from './EvaluationCard';
import type { ChatMessage } from '../../types';
import { t } from '../../i18n';
import { RichTextField } from './RichTextField';
import { messageClipboard } from './messageClipboard';
import { copyHtmlToClipboard } from '../../utils/clipboard';
import { message } from 'antd';
import { ForkChatButton } from './ForkChatButton';
import { formatTurnSeconds } from '../../utils/elapsed';

/** Format a message timestamp as local "YYYY-MM-DD HH:MM:SS", shown below each message. */
function formatMsgTime(ts: number): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/**
 * The settled turn time as "用时 41秒" / "用时 1分05秒" — whole seconds, the same
 * shape the live clock used while the reply streamed, so it doesn't change form
 * at the end. The exact value lives in the tooltip.
 */
function formatDuration(ms?: number): { label: string; exact: string } | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  return {
    label: t('用时 {d}', { d: formatTurnSeconds(Math.round(ms / 1000)) }),
    exact: t('本次回答整体生成耗时 {sec} 秒', { sec: (ms / 1000).toFixed(1) }),
  };
}

/** Save a quick like / dislike (store is optimistic and rolls back on failure). */
function saveVerdict(chatId: string, messageId: string, verdict: 'adopt' | 'reject', comment: string) {
  useEvaluationStore.getState().submit(chatId, messageId, { verdict, reasons: [], comment })
    .catch(() => { void message.error(t('评价没有保存，请稍后再试')); });
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
    // preventScroll：原生 focus 会把控件滚入视口，叠加展开动画会把整个列表拽到底部
    const id = window.setTimeout(() => ref.current?.focus({ cursor: 'end', preventScroll: true }), EXPAND_FOCUS_DELAY_MS);
    return () => window.clearTimeout(id);
  }, [open, ref]);
}

interface MessageActionsProps {
  m: ChatMessage;
  messageIndex: number;
  currentChatId: string;
  exportChatRecord: (id: string) => Promise<void>;
  regenerate?: (messageIndex: number) => void;
  editAndResend?: (messageIndex: number, newContent: string) => void;
}
export function MessageActions({ m, messageIndex, currentChatId, exportChatRecord, regenerate, editAndResend }: MessageActionsProps) {
  const duration = m.role === 'assistant' ? formatDuration(m.durationMs) : null;
  const copiedMsg = useChatStore((s) => s.copiedMsg);
  const setCopiedMsg = useChatStore((s) => s.setCopiedMsg);
  // With the 会话评价 permission the answer gets the evaluation card; without it, the quick
  // like / dislike buttons record 满意 / 不满意.
  const evaluationEnabled = useAuthStore((s) => s.authUser?.can_use_evaluation === true);
  // Scheduled and loop tasks have no evaluation at all: no card, no like / dislike.
  const excludedAnswer = useEvaluationStore((s) => !!m.messageId && !!s.excluded[m.messageId]);
  const scheduledChat = useChatStore((s) => isAutomationHistoryChat(s.store.chats[currentChatId]));
  const evaluable = !excludedAnswer && !scheduledChat;
  const verdict = useEvaluationStore((s) => (m.messageId ? s.records[m.messageId]?.verdict : undefined));
  const storedComment = useEvaluationStore((s) => (m.messageId ? s.records[m.messageId]?.comment ?? '' : ''));
  const [isDisliking, setIsDisliking] = useState(false);
  const [dislikeComment, setDislikeComment] = useState('');
  // A new dislike is saved once, when its note form closes (提交 / 跳过 / dislike again / like /
  // the message unmounting), so the verdict and the note go out in a single request.
  const pendingDislike = useRef<{ chatId: string; messageId: string; comment: string } | null>(null);
  const shareSelectionMode = useChatStore((s) => s.shareSelectionMode);
  const startShareSelectionWithAll = useChatStore((s) => s.startShareSelectionWithAll);
  const editingMessageUid = useChatStore((s) => s.editingMessageUid);
  const setEditingMessageUid = useChatStore((s) => s.setEditingMessageUid);
  const [editText, setEditText] = useState('');
  const isEditing = editingMessageUid === m.uid;
  const showEvaluationCard = evaluationEnabled && evaluable && m.role === 'assistant' && !m.isStreaming && !m.inFlight
    && !m.error && !!m.messageId && !isEditing;
  const openDislike = () => {
    if (!m.messageId) return;
    const rejected = verdict === 'reject';
    // Already 不满意: the form only edits the note, so closing it without a new note saves nothing.
    pendingDislike.current = rejected ? null : { chatId: currentChatId, messageId: m.messageId, comment: '' };
    setDislikeComment(rejected ? storedComment : '');
    setIsDisliking(true);
  };
  /** `note` is the typed note on 提交, `null` when the form is skipped or toggled closed. */
  const closeDislike = (note: string | null) => {
    const pending = pendingDislike.current;
    pendingDislike.current = null;
    setIsDisliking(false);
    const text = note?.trim() ?? '';
    if (pending) {
      saveVerdict(pending.chatId, pending.messageId, 'reject', text);
    } else if (text && text !== storedComment.trim() && m.messageId && verdict === 'reject') {
      saveVerdict(currentChatId, m.messageId, 'reject', text);
    }
  };
  const like = () => {
    // Liking with the dislike form open drops the unsaved dislike instead of saving it first.
    if (isDisliking) {
      pendingDislike.current = null;
      setIsDisliking(false);
    }
    if (verdict !== 'adopt' && m.messageId) saveVerdict(currentChatId, m.messageId, 'adopt', '');
  };
  // Leaving the chat (or regenerating) with the form still open keeps the dislike and its draft note.
  useEffect(() => () => {
    const pending = pendingDislike.current;
    pendingDislike.current = null;
    if (pending) saveVerdict(pending.chatId, pending.messageId, 'reject', pending.comment.trim());
  }, []);
  const disliked = isDisliking || verdict === 'reject' || verdict === 'revise';
  const liked = verdict === 'adopt' && !isDisliking;

  const dislikeInputRef = useRef<TextAreaRef>(null);

  // Edit box / dislike form: lazy-keep-mounted (only mount on first open + jx-expandWrap class-toggle for the height animation),
  // autoFocus is not triggered by mounting — focus is delayed until after expanding.
  const editExpand = useLazyExpand(isEditing);
  const dislikeExpand = useLazyExpand(isDisliking);
  useExpandFocus(isDisliking, dislikeInputRef);
  const doCopy = async () => {
    const { html, plainText } = messageClipboard(m);
    if (!await copyHtmlToClipboard(html, plainText)) {
      void message.error(t('复制失败'));
      return;
    }
    setCopiedMsg(m.uid);
    setTimeout(() => {
      if (useChatStore.getState().copiedMsg === m.uid) setCopiedMsg(null);
    }, 2000);
  };

  return <>
        {/* User message editing — lazy-keep-mounted: only mount on first open, then keep it to play the collapse animation */}
        {m.role === 'user' && !!editAndResend && editExpand.mounted && (
          <div className={`jx-expandWrap jx-msgExpand${editExpand.openClass ? ' jx-expandWrap--open' : ''}`}>
            <div className="jx-editMessage">
              <RichTextField
                value={editText}
                onChange={setEditText}
                active={isEditing}
                label={t('编辑消息')}
                className="jx-editMessage-input"
                onSubmit={() => {
                  if (editText.trim()) editAndResend?.(messageIndex, editText.trim());
                }}
                onCancel={() => setEditingMessageUid(null)}
              />
              <div className="jx-editMessage-btns">
                <Button size="small" onClick={() => setEditingMessageUid(null)}>{t('取消')}</Button>
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
            {/* 整轮耗时放在最前：按钮未 hover 时是透明占位，放后面会被它们推出一段空白。 */}
            {duration && (
              <span className="jx-msgDuration" title={duration.exact}>{duration.label}</span>
            )}
            <button className={`jx-msgActionBtn${copiedMsg === m.uid ? ' copied' : ''}`}
              title={copiedMsg === m.uid ? t('已复制') : t('复制内容')}
              onClick={() => { void doCopy(); }}>
              {copiedMsg === m.uid ? <CheckOutlined /> : <CopyOutlined />}
            </button>
            {m.role === 'user' && editAndResend && (
              <button className="jx-msgActionBtn" title={t('编辑消息')}
                onClick={() => {
                  setEditText(m.content);
                  setEditingMessageUid(m.uid);
                }}>
                <EditOutlined />
              </button>
            )}
            {m.role === 'assistant' && (<>
              {!evaluationEnabled && evaluable && (<>
                <button className={`jx-msgActionBtn${liked ? ' active-like' : ''}`} title={t('有帮助')}
                  aria-label={t('有帮助')} aria-pressed={liked}
                  onClick={like}>
                  {liked ? <LikeFilled /> : <LikeOutlined />}
                </button>
                <button className={`jx-msgActionBtn${disliked ? ' active-dislike' : ''}`} title={t('没有帮助')}
                  aria-label={t('没有帮助')} aria-pressed={disliked}
                  onClick={() => { if (isDisliking) closeDislike(null); else openDislike(); }}>
                  {disliked ? <DislikeFilled /> : <DislikeOutlined />}
                </button>
              </>)}
              {m.messageId && (
                <ForkChatButton chatId={currentChatId} messageId={m.messageId} />
              )}
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
                  // 注意读的是**整份** messages，不是上面那份切到本条为止的订阅切片
                  // （那份是为了让 memo 生效而特意收窄的，拿它全选会漏掉后面的消息）。
                  const all = useChatStore.getState().store.chats[currentChatId]?.messages ?? [];
                  startShareSelectionWithAll(
                    all.filter((msg) => !msg.isStreaming).map((msg) => msg.uid),
                  );
                }}
              >
                <ShareAltOutlined />
              </button>
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

        {showEvaluationCard && m.messageId && (
          <EvaluationCard chatId={currentChatId} messageId={m.messageId} />
        )}

        {/* Dislike feedback form — lazy-keep-mounted: only mount on first open, then keep it to play the collapse animation */}
        {m.role === 'assistant' && dislikeExpand.mounted && (
          <div className={`jx-expandWrap jx-msgExpand${dislikeExpand.openClass ? ' jx-expandWrap--open' : ''}`}>
            <div className="jx-dislikeFeedback">
              <p className="jx-dislikeFeedback-title">{t('请告诉我们哪里不好（可选）')}</p>
              <Input.TextArea ref={dislikeInputRef} rows={3} placeholder={t('内容不准确 / 答非所问 / 其他...')}
                value={dislikeComment} className="jx-dislikeFeedback-input"
                onChange={(e) => {
                  setDislikeComment(e.target.value);
                  if (pendingDislike.current) pendingDislike.current.comment = e.target.value;
                }} />
              <div className="jx-dislikeFeedback-btns">
                <Button size="small" onClick={() => closeDislike(null)}>{t('跳过')}</Button>
                <Button size="small" type="primary" onClick={() => closeDislike(dislikeComment)}>{t('提交')}</Button>
              </div>
            </div>
          </div>
        )}
  </>;
}
