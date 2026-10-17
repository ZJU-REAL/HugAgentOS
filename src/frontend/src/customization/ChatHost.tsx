import { type ReactNode } from 'react';
import { ChatRuntimeProvider } from '@hugagent/chat-sdk/react';
import type { useAppController } from '../hooks/useAppController';
import { ChatCustomizationRoot } from './ChatCustomization';
import { useHostChatRuntime } from './useChatRuntime';

export function ChatHost({ state, children }: {
  state: ReturnType<typeof useAppController>;
  children: ReactNode;
}) {
  const runtime = useHostChatRuntime(state);
  return <ChatRuntimeProvider value={runtime}>
    <ChatCustomizationRoot>{children}</ChatCustomizationRoot>
  </ChatRuntimeProvider>;
}
