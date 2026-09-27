import { LoadingOutlined } from '@ant-design/icons';
import { useChatFork } from '../../hooks/useChatFork';
import { t } from '../../i18n';
import { ForkChatIcon } from './ForkChatIcon';

export function ForkChatButton({ chatId, messageId, disabled = false }: {
  chatId: string;
  messageId: string;
  disabled?: boolean;
}) {
  const { forkChat, pending } = useChatFork(chatId);
  return (
    <button type="button" className="jx-msgActionBtn" title={t('创建聊天分支')}
      aria-label={t('创建聊天分支')} aria-busy={pending} disabled={disabled || pending}
      onClick={() => { void forkChat(messageId); }}>
      {pending ? <LoadingOutlined /> : <ForkChatIcon />}
    </button>
  );
}
