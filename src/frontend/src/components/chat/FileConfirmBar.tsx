import { useState } from 'react';
import { message } from 'antd';
import { AnimatePresence, motion } from 'motion/react';
import { SafetyCertificateFilled, CloseOutlined } from '@ant-design/icons';
import { EASE } from '../../utils/motionTokens';
import { useChatStore, useUIStore } from '../../stores';
import { confirmFileWrite } from '../../api';
import type { FileConfirmDecision } from '../../types';
import { t } from '../../i18n';

const OP_LABEL: Record<string, string> = {
  write: t('写入文件'),
  edit: t('修改文件'),
  delete: t('删除文件'),
  move: t('移动文件'),
  mkdir: t('创建文件夹'),
  // Automation (scheduled task) changes (kind === 'automation')
  cron_create: t('创建定时任务'),
  cron_update: t('修改定时任务'),
  cron_delete: t('删除定时任务'),
};

/**
 * §13 My Space write-operation confirmation bar — floats above the input box (modeled on
 * the desktop permission-confirmation float bar).
 *
 * Shape: the tool coroutine that triggered the write is right now **suspended** in the
 * backend (`await event.wait()` in `_myspace_confirm.gate`); the original SSE stream has not
 * ended, it just temporarily has no new events. When the user clicks "Allow/Deny" → an
 * out-of-band `POST /file-confirm` wakes that coroutine: allow → the tool actually performs
 * this write once, in place; deny → the tool returns a rejection. Subsequent tool_result /
 * meta still keep coming from **that same original SSE stream** — no user message is re-sent,
 * no resume run is started, the model does not retry.
 */
export function FileConfirmBar() {
  const currentChatId = useChatStore((s) => s.currentChatId);
  const queue = useUIStore((s) => s.pendingConfirm[currentChatId]);
  const enqueuePendingConfirm = useUIStore((s) => s.enqueuePendingConfirm);
  const resolvePendingConfirm = useUIStore((s) => s.resolvePendingConfirm);
  const clearPendingConfirm = useUIStore((s) => s.clearPendingConfirm);
  const [busy, setBusy] = useState(false);

  // The queue head is the item currently awaiting confirmation; the rest count how many are
  // queued behind it. One round of parallel tool calls registers N of them concurrently —
  // they pop one by one, the next appears automatically after the head is decided, none lost.
  // (The queue only holds approve/deny-semantic kinds; design_pick is split off at the entry
  // into the pendingDesignPick single slot, rendered by DesignPickerCard, and never enters here.)
  const info = queue && queue.length ? queue[0] : undefined;
  const remaining = info ? queue.length - 1 : 0;
  const isAuto = info?.kind === 'automation';
  // automation is located by summary (message), myspace by logical path.
  const detail = info ? (isAuto ? info.message || '' : info.logicalPath) : '';

  const decide = async (decision: FileConfirmDecision) => {
    if (busy || !info) return;
    setBusy(true);
    const head = info;
    // Optimistically dequeue the head; on failure re-append to the tail for the user to retry.
    // Both allow/deny merely wake the suspended tool coroutine; the real execution result comes
    // back from the original SSE stream as a tool_result.
    resolvePendingConfirm(currentChatId, head.confirmId);
    try {
      const res = await confirmFileWrite(currentChatId, head.confirmId, decision);
      if (res.stale) {
        // Already dequeued, just give an explanation, don't re-append — re-appending would
        // still fall through when the user clicks again.
        // Plan F mid-stage: the backend distinguishes "ordinary timeout" from "agent task died
        // due to server restart". The latter must re-send the message, otherwise the file never
        // gets written — so use message.warning + a more specific description so the user
        // immediately realizes they must resend.
        if (res.chat_interrupted) {
          message.warning({
            content: res.message || t('上次会话因服务端重启未完成，请重新发送您的消息'),
            duration: 6,
          });
        } else {
          message.info(t('该确认已过期（可能超时），已为你关闭。如仍需要请重新发起。'));
        }
      } else if (decision === 'allow_session') {
        // "Allow all this session": the backend has cascade-released all current pending items
        // and set session_allow for subsequent writes. The frontend clears the whole queue at
        // once instead of popping one by one — otherwise remaining items still show waiting for
        // the user to click (even though the backend already released them), appearing as
        // "still confirming after clicking".
        clearPendingConfirm(currentChatId);
        message.success(
          head.kind === 'automation'
            ? t('已允许本次会话的全部定时任务操作，后续不再逐个确认。')
            : t('已允许本次会话的全部「我的空间」写操作，后续不再逐个确认。'),
        );
      } else if (decision === 'deny') {
        message.info(
          head.kind === 'automation'
            ? t('已拒绝该操作，定时任务未改动。')
            : t('已拒绝该操作，文件未改动。如需临时产物可让助手写到 /workspace。'),
        );
      }
    } catch (e: unknown) {
      // Real failure (network / invalid decision, etc.): re-append this item for the user to retry
      enqueuePendingConfirm(currentChatId, head);
      message.error(t('操作失败：{msg}', { msg: e instanceof Error ? e.message : String(e) }));
    } finally {
      setBusy(false);
    }
  };

  // Entry keeps the .jx-confirmBar CSS animation; motion only adds two things:
  // ① the exit of the whole confirmation bar (AnimatePresence + initial={false}, to avoid double-playing with the CSS entry)
  // ② when confirming the queue item by item, crossfade the body by confirmId (card-swap feel)
  return (
    <AnimatePresence initial={false}>
      {info && (
        <motion.div
          key="fileConfirmBar"
          exit={{ opacity: 0, y: 8, transition: { duration: 0.15, ease: EASE.exit } }}
        >
          <div
            className="jx-confirmBar"
            role="alertdialog"
            aria-label={isAuto ? t('定时任务操作确认') : t('我的空间写操作确认')}
          >
            <span className="jx-confirmBar-icon" aria-hidden="true">
              <SafetyCertificateFilled />
            </span>
            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                key={info.confirmId}
                className="jx-confirmBar-body"
                initial={{ opacity: 0, x: 6 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -6 }}
                transition={{ duration: 0.15, ease: EASE.standard }}
              >
                <div className="jx-confirmBar-title">
                  {isAuto
                    ? t('需要你确认一项定时任务操作')
                    : t('需要你确认一项「我的空间」写操作')}
                  {remaining > 0 && (
                    <span className="jx-confirmBar-count">
                      {t('（还有 {n} 项排队，逐个确认）', { n: remaining })}
                    </span>
                  )}
                </div>
                <div className="jx-confirmBar-detail">
                  <span className="jx-confirmBar-op">{OP_LABEL[info.op] || info.op}</span>
                  <span className="jx-confirmBar-path" title={detail}>
                    {detail}
                  </span>
                </div>
                <div className="jx-confirmBar-hint">
                  {isAuto
                    ? t('该操作会更改你的定时任务安排。确认后助手会在本对话里直接继续完成任务。')
                    : t('该操作会修改你的个人网盘（跨会话永久保存）。确认后助手会在本对话里直接继续完成任务。')}
                </div>
              </motion.div>
            </AnimatePresence>
            <div className="jx-confirmBar-actions">
              <button
                type="button"
                className="jx-confirmBar-btn jx-confirmBar-btn--ghost"
                disabled={busy}
                onClick={() => decide('deny')}
              >
                <CloseOutlined className="jx-confirmBar-btnIcon" />
                {t('拒绝')}
              </button>
              <button
                type="button"
                className="jx-confirmBar-btn jx-confirmBar-btn--soft"
                disabled={busy}
                onClick={() => decide('allow_session')}
              >
                {t('本次会话都允许')}
              </button>
              <button
                type="button"
                className="jx-confirmBar-btn jx-confirmBar-btn--primary"
                disabled={busy}
                onClick={() => decide('allow')}
              >
                {t('允许并继续')}
              </button>
            </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
