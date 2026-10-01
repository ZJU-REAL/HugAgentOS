import { Button, Input } from 'antd';
import type { TextAreaRef } from 'antd/es/input/TextArea';
import React, { useEffect, useRef, useState } from 'react';
import {
  CopyOutlined, CheckOutlined, LikeOutlined, DislikeOutlined, LikeFilled, DislikeFilled,
  ExportOutlined, ShareAltOutlined, EditOutlined, SyncOutlined,
} from '@ant-design/icons';
import { authFetch } from '../../api';
import { useChatStore } from '../../stores';
import type { ChatMessage } from '../../types';
import { t } from '../../i18n';
import { RichTextField } from './RichTextField';
import { userMarkdownHtml } from '../../utils/userMarkdown';
import { copyHtmlToClipboard } from '../../utils/clipboard';
import { message } from 'antd';
import { ForkChatButton } from './ForkChatButton';
const effectiveApiUrl = (import.meta.env.VITE_API_BASE_URL as string || '').trim() || '/api';

/** Format a message timestamp as local "YYYY-MM-DD HH:MM:SS", shown below each message. */
function formatMsgTime(ts: number): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Format the total generation time (milliseconds) as "用时 X.X秒"; when over 1 minute, use "用时 N分M秒". 单位统一用「秒」。 */
function formatDuration(ms?: number): string | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 60_000) return t('用时 {sec}秒', { sec: (ms / 1000).toFixed(1) });
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
  const copiedMsg = useChatStore((s) => s.copiedMsg);
  const setCopiedMsg = useChatStore((s) => s.setCopiedMsg);
  const feedbackMap = useChatStore((s) => s.feedbackMap);
  const setFeedbackMap = useChatStore((s) => s.setFeedbackMap);
  const dislikingUid = useChatStore((s) => s.dislikingUid);
  const setDislikingUid = useChatStore((s) => s.setDislikingUid);
  const dislikeComment = useChatStore((s) => s.dislikeComment);
  const setDislikeComment = useChatStore((s) => s.setDislikeComment);
  const shareSelectionMode = useChatStore((s) => s.shareSelectionMode);
  const startShareSelectionWithAll = useChatStore((s) => s.startShareSelectionWithAll);
  const editingMessageUid = useChatStore((s) => s.editingMessageUid);
  const setEditingMessageUid = useChatStore((s) => s.setEditingMessageUid);
  const [editText, setEditText] = useState('');
  const isEditing = editingMessageUid === m.uid;
  const isDisliking = dislikingUid === m.uid;

  const dislikeInputRef = useRef<TextAreaRef>(null);

  // Edit box / dislike form: lazy-keep-mounted (only mount on first open + jx-expandWrap class-toggle for the height animation),
  // autoFocus is not triggered by mounting — focus is delayed until after expanding.
  const editExpand = useLazyExpand(isEditing);
  const dislikeExpand = useLazyExpand(isDisliking);
  useExpandFocus(isDisliking, dislikeInputRef);
  const messagePlainText = m.segments
    ? m.segments.filter(s => s.type === 'text').map(s => s.content || '').join('\n\n') || m.content
    : m.content;
  /** Clipboard carries canonical Markdown and a formatted HTML representation. */
  const doCopy = async (str: string) => {
    const host = document.createElement('div');
    if (m.role === 'user' || m.isMarkdown) host.innerHTML = userMarkdownHtml(str);
    else host.textContent = str;
    host.querySelectorAll('button').forEach(node => node.remove());
    if (!await copyHtmlToClipboard(host.innerHTML, str)) {
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
            <button className={`jx-msgActionBtn${copiedMsg === m.uid ? ' copied' : ''}`}
              title={copiedMsg === m.uid ? t('已复制') : t('复制内容')}
              onClick={() => { void doCopy(messagePlainText); }}>
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
              <button className={`jx-msgActionBtn${feedbackMap[m.uid] === 'like' ? ' active-like' : ''}`} title={t('有帮助')}
                onClick={() => {
                  const next = feedbackMap[m.uid] === 'like' ? undefined : 'like' as const;
                  setFeedbackMap(next ? { ...feedbackMap, [m.uid]: next } : Object.fromEntries(Object.entries(feedbackMap).filter(([k]) => k !== m.uid)));
                  if (next && m.messageId) {
                    authFetch(`${effectiveApiUrl}/v1/chats/messages/${m.messageId}/feedback`, {
                      method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ rating: 'like', chat_id: currentChatId }),
                    }).catch(() => {});
                  }
                }}>
                {feedbackMap[m.uid] === 'like' ? <LikeFilled /> : <LikeOutlined />}
              </button>
              <button className={`jx-msgActionBtn${feedbackMap[m.uid] === 'dislike' ? ' active-dislike' : ''}`} title={t('没有帮助')}
                onClick={() => {
                  if (feedbackMap[m.uid] === 'dislike') {
                    setFeedbackMap(Object.fromEntries(Object.entries(feedbackMap).filter(([k]) => k !== m.uid)));
                    setDislikingUid(null);
                  } else {
                    setFeedbackMap({ ...feedbackMap, [m.uid]: 'dislike' });
                    setDislikingUid(m.uid);
                    setDislikeComment('');
                  }
                }}>
                {feedbackMap[m.uid] === 'dislike' ? <DislikeFilled /> : <DislikeOutlined />}
              </button>
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
                <Button size="small" onClick={() => setDislikingUid(null)}>{t('跳过')}</Button>
                <Button size="small" type="primary" onClick={() => {
                  if (m.messageId) {
                    authFetch(`${effectiveApiUrl}/v1/chats/messages/${m.messageId}/feedback`, {
                      method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ rating: 'dislike', comment: dislikeComment || undefined, chat_id: currentChatId }),
                    }).catch(() => {});
                  }
                  setDislikingUid(null);
                }}>{t('提交')}</Button>
              </div>
            </div>
          </div>
        )}
  </>;
}
