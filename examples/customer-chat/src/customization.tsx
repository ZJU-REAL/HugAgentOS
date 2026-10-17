import { defineChatCustomization, useChatRuntime, type ChatSlotProps, type MessageSlotProps } from '@hugagent/chat-sdk/react';
import './theme.css';

function Welcome({ children }: ChatSlotProps) {
  const { view, actions } = useChatRuntime();
  return <section className="customer-welcome" data-testid="customer-welcome">
    <header><strong>Customer Portal</strong><span>{view.productName}</span>
      <button type="button" onClick={() => actions.setDraft('Please summarize this document.')}>Use a template</button>
    </header>
    {children}
  </section>;
}
function Message({ message, children }: MessageSlotProps) {
  return <div className="customer-message" data-testid="customer-message" data-role={message.role}>
    <span className="customer-message-label">{message.role === 'assistant' ? 'Portal Assistant' : 'You'}</span>
    {children}
  </div>;
}
function Composer({ children }: ChatSlotProps) {
  const { view } = useChatRuntime();
  return <section className="customer-composer" aria-label="Portal composer">
    <span className="customer-composer-status" role="status">{view.conversation.sending ? 'Working' : 'Ready'}</span>
    {children}
  </section>;
}
export default defineChatCustomization({
  apiVersion: 1, id: 'customer-portal',
  slots: { welcome: Welcome, message: Message, composer: Composer },
});
