import { memo, useMemo, useState } from 'react';
import { motion } from 'motion/react';
import { FileAttachmentCard } from '../file';
import { ToolMessageContext } from '../tool/ToolMessageContext';
import { EvolutionCard } from './EvolutionCard';
import { OntologyReviewTrigger } from './OntologyReviewTrigger';
import { MessageBody } from './MessageBody';
import { MessageActions } from './MessageActions';
import { MessageArtifacts } from './MessageArtifacts';
import { MessageInheritedCards } from './MessageInheritedCards';
import { useMessageSelection } from './useMessageSelection';
import { useChatStore } from '../../stores';
import type { ChatMessage } from '../../types';
import { isForkedHistory } from '../../utils/forkHistory';
import { FRESH_ENTER_WINDOW_MS } from '../../utils/motionTokens';
import { t } from '../../i18n';
const effectiveApiUrl = (import.meta.env.VITE_API_BASE_URL as string || '').trim() || '/api';
interface MessageBubbleProps {
  m: ChatMessage;
  messageIndex: number;
  currentChatId: string;
  send: (text?: string) => void;
  exportChatRecord: (id: string) => Promise<void>;
  regenerate?: (messageIndex: number) => void;
  editAndResend?: (messageIndex: number, newContent: string) => void;
}

/**
 * 一条消息气泡。
 *
 * `memo` 不是锦上添花：不包的话，模型每吐一个字（store 一变）整条对话的**每一条**
 * 气泡都要重新渲染一遍，长对话里这笔开销随消息数线性上涨。生效的前提有两个，都已
 * 在上下文里做掉了——回调由 ChatArea 用 useStableCallback 稳住身份，store 逐项订阅
 * 且 messages 只订到本条为止。
 */
export const MessageBubble = memo(function MessageBubble({ m, messageIndex, currentChatId, send, exportChatRecord, regenerate, editAndResend }: MessageBubbleProps) {
  const { contentRef, onMouseDown, onMouseUp, selectionMenu } = useMessageSelection(m);
  const shareSelectionMode = useChatStore((s) => s.shareSelectionMode);
  const selectedShareMessageUids = useChatStore((s) => s.selectedShareMessageUids);
  const toggleShareMessageUid = useChatStore((s) => s.toggleShareMessageUid);
  const shareSelected = selectedShareMessageUids.has(m.uid);
  const sending = useChatStore((s) => s.sending);
  const inherited = isForkedHistory(m);
  const [isFresh] = useState(() => Date.now() - m.ts < FRESH_ENTER_WINDOW_MS);
  const toolMessageIdentity = useMemo(
    () => ({ chatId: currentChatId, messageId: m.messageId, messageUid: m.uid, readOnly: inherited }),
    [currentChatId, m.messageId, m.uid, inherited],
  );

  return (
    // 工具卡要知道自己属于哪条消息，才能在展开时按需取回完整结果
    // （历史列表只下发梗概）。挂在这里一次，省得逐层透传 chatId/messageId。
    <ToolMessageContext.Provider value={toolMessageIdentity}>
    <div
      className={`jx-msg ${m.role === 'user' ? 'user' : 'assistant'}${isFresh ? ' jx-msg--fresh' : ''}`}
      data-message-ts={m.ts}
      data-message-uid={m.uid}
    >
      <div className={`jx-msgInner${m.role === 'user' ? ' user' : ''}${shareSelectionMode && !m.isStreaming ? ' share-selectable' : ''}`}>
        {shareSelectionMode && !m.isStreaming && (
          <label className="jx-shareCheckboxWrap" aria-label={t('选择这条对话记录')}>
            <input
              type="checkbox"
              className="jx-shareCheckbox"
              checked={shareSelected}
              onChange={() => toggleShareMessageUid(m.uid)}
            />
          </label>
        )}
      <div
        className="jx-msgContent"
        ref={contentRef}
        onMouseDown={onMouseDown}
        onMouseUp={onMouseUp}
      >
        {/* User attachments */}
        {m.role === 'user' && m.attachments && m.attachments.length > 0 && (
          <div className="jx-userAttachments">
            {m.attachments.map((att, idx) => (
              <FileAttachmentCard
                key={idx}
                name={att.name}
                downloadHref={(att.download_url || att.file_id) ? `${effectiveApiUrl}${att.download_url || `/files/${att.file_id}`}` : undefined}
                artifact={att.file_id ? {
                  file_id: att.file_id,
                  origin: att.origin,
                  name: att.name,
                  url: att.download_url || `/files/${att.file_id}`,
                  mime_type: att.mime_type,
                  chat_id: currentChatId,
                } : undefined}
              />
            ))}
          </div>
        )}

        <MessageBody m={m} messageIndex={messageIndex} currentChatId={currentChatId} send={send} />
        {/* Artifact cards */}
        {m.role === 'assistant' && <MessageArtifacts m={m} currentChatId={currentChatId} />}

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

        {m.role === 'assistant' && m.evolution && !inherited && (
          <EvolutionCard
            summary={m.evolution}
            chatId={currentChatId}
            messageId={m.messageId}
          />
        )}

        {m.role === 'assistant' && m.ontologyGovernance && !inherited && (
          <div className="jx-ontologyReviewInline">
            <OntologyReviewTrigger
              governance={m.ontologyGovernance}
              chatId={currentChatId}
              messageUid={m.uid}
            />
          </div>
        )}

        {inherited && <MessageInheritedCards m={m} />}
        {selectionMenu}
        <MessageActions m={m} messageIndex={messageIndex} currentChatId={currentChatId}
          exportChatRecord={exportChatRecord} regenerate={regenerate} editAndResend={editAndResend} />
      </div>
      </div>
    </div>
    </ToolMessageContext.Provider>
  );
});
