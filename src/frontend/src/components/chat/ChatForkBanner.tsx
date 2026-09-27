import { useEffect, useState } from 'react';
import { getChatDetail, isLocalChat, registerLocalChat } from '../../api';
import type { ChatForkOrigin } from '../../api/chatForks';
import { useChatStore } from '../../stores/chatStore';
import { t } from '../../i18n';
import { ForkChatIcon } from './ForkChatIcon';
import '../../styles/chat-fork.css';

/** Source metadata is a historical label; access to the original is checked separately. */
export function ChatForkBanner({ chatId }: { chatId: string }) {
  const actor = useChatStore((state) => state.currentUserId);
  const [state, setState] = useState<{ chatId: string; actor: string | null; origin: ChatForkOrigin; accessible: boolean } | null>(null);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const detail = await getChatDetail(chatId);
        const value = detail.metadata?.fork;
        if (!value || typeof value !== 'object' || cancelled) return;
        const origin = value as ChatForkOrigin;
        if (typeof origin.source_chat_id !== 'string' || typeof origin.source_title !== 'string') return;
        setState({ chatId, actor, origin, accessible: false });
        if (isLocalChat(chatId)) registerLocalChat(origin.source_chat_id);
        try {
          await getChatDetail(origin.source_chat_id);
          if (!cancelled) setState({ chatId, actor, origin, accessible: true });
        } catch { /* A deleted or inaccessible source remains a non-interactive historical label. */ }
      } catch { /* Ordinary chats and unavailable metadata need no banner. */ }
    })();
    return () => { cancelled = true; };
  }, [chatId, actor]);
  if (!state || state.chatId !== chatId || state.actor !== actor) return null;
  return (
    <div className="jx-chatForkBanner" role="status">
      <ForkChatIcon />
      <span>{t('分支自：')}</span>
      {state.accessible ? (
        <button type="button" title={t('打开原聊天')}
          onClick={() => useChatStore.getState().setCurrentChatId(state.origin.source_chat_id)}>
          {state.origin.source_title || t('原聊天')}
        </button>
      ) : <span>{state.origin.source_title || t('原聊天')}</span>}
      <span className="jx-chatForkBanner-note">{t('历史消息独立保存，附件保留原文件引用')}</span>
    </div>
  );
}
