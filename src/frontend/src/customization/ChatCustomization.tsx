import { type ReactNode } from 'react';
import { validateChatCustomization, useOptionalChatRuntime, type ChatSlotProps, type MessageView } from '@hugagent/chat-sdk/react';
import customization from './selected';

function selected() {
  if (customization === null) return null;
  validateChatCustomization(customization);
  return customization;
}
/** Called inside the application error boundary so invalid contracts render an error. */
export function ChatSlot({ name, children, message }: ChatSlotProps & {
  name: 'welcome' | 'composer' | 'message' | 'sidebar' | 'conversation';
  message?: MessageView;
}) {
  const runtime = useOptionalChatRuntime();
  if (!runtime) return children;
  const slots = selected()?.slots;
  if (name === 'message') {
    const Component = slots?.message;
    return Component && message ? <Component message={message}>{children}</Component> : children;
  }
  const Component = slots?.[name];
  return Component ? <Component>{children}</Component> : children;
}
export function ChatCustomizationRoot({ children }: { children: ReactNode }) {
  let id: string;
  try { id = selected()?.id || 'default'; }
  catch (error) {
    return <section role="alert"><h1>Chat customization failed</h1>
      <p>{error instanceof Error ? error.message : 'Invalid customization contract'}</p></section>;
  }
  return <div className="jx-customizationRoot" data-chat-customization={id} style={{ display: 'contents' }}>{children}</div>;
}
