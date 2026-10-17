import { useEffect, useRef, useState, type ReactNode } from 'react';
import { ChatRuntimeProvider, validateChatCustomization, type ChatActions, type ChatCustomization, type ChatView } from './react.js';

const initial = (): ChatView => ({
 productName: 'HugAgentOS Preview', draft: '', attachments: [], confirmations: [], questions: [],
 conversation: { id: 'preview-1', title: 'Preview conversation', messages: [], sending: false },
 conversations: [{ id: 'preview-1', title: 'Preview conversation' }],
});
/** Offline contract host. Its sample renderer does not reproduce the full product UI. */
export function PreviewHost({ customization }: { customization: ChatCustomization }) {
 validateChatCustomization(customization);
 const [view, setView] = useState(initial);
 const [status, setStatus] = useState('Offline preview. No requests are sent.');
 const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
 const generation = useRef(0);
 useEffect(() => () => { generation.current++; clearTimeout(timer.current); }, []);
 const slots = customization.slots || {};
 const patch = (update: (current: ChatView) => ChatView) => setView(update);
 const send = (text = view.draft) => {
  if (!text.trim() || view.conversation.sending) return;
  const epoch = ++generation.current;
  patch(v => ({ ...v, draft: '', attachments: [], conversation: { ...v.conversation, sending: true,
   messages: [...v.conversation.messages, { id: String(Date.now()), role: 'user', content: text, streaming: false }] } }));
  timer.current = setTimeout(() => {
   if (epoch !== generation.current) return;
   patch(v => ({ ...v, conversation: { ...v.conversation, sending: false, messages: [...v.conversation.messages,
    { id: String(Date.now()), role: 'assistant', content: 'Preview reply: ' + text, streaming: false,
      thinking: ['Sample reasoning'], citations: [{ id: 'sample', title: 'Sample citation', url: '', snippet: 'Preview only' }],
      tools: [{ name: 'sample-tool', status: 'completed' }] }] } }));
  }, 500);
 };
 const reset = () => { generation.current++; clearTimeout(timer.current); setView(initial()); };
 const actions: ChatActions = {
  send, setDraft: draft => patch(v => ({ ...v, draft })),
  cancel: () => { generation.current++; clearTimeout(timer.current); patch(v => ({ ...v, conversation: { ...v.conversation, sending: false } })); setStatus('Preview cancelled'); },
  newChat: reset, selectChat: reset,
  attachFiles: files => patch(v => ({ ...v, attachments: [...v.attachments, ...files.map((f, i) => ({ index: v.attachments.length + i, name: f.name, size: f.size, uploading: false }))] })),
  removeFile: index => patch(v => ({ ...v, attachments: v.attachments.filter(f => f.index !== index).map((f, i) => ({ ...f, index: i })) })),
  retryFile: () => setStatus('Preview files stay on this device. Use Live chat to test uploads.'),
  regenerate: () => send('Regenerated preview reply'),
  editAndResend: (_id, text) => send(text),
  confirm: async id => { patch(v => ({ ...v, confirmations: v.confirmations.filter(c => c.id !== id), designPick: null })); return { ok: true }; },
  answer: async id => { patch(v => ({ ...v, questions: v.questions.filter(q => q.requestId !== id) })); return { ok: true }; },
 };
 const wrap = (name: 'welcome' | 'composer' | 'sidebar' | 'conversation', children: ReactNode) => {
  const Slot = slots[name]; return Slot ? <Slot>{children}</Slot> : children;
 };
 return <ChatRuntimeProvider value={{ view, actions }}><div data-chat-customization={customization.id}>
  <p role="status">{status}</p>
  <div className="preview-layout">
   <aside>{wrap('sidebar', <><h2>Conversations</h2><button onClick={reset}>New preview chat</button>
    {view.conversations.map(c => <button key={c.id} onClick={() => actions.selectChat(c.id)}>{c.title}</button>)}</>)}</aside>
   <section>{wrap('conversation', <>
    {view.conversation.messages.length === 0 && wrap('welcome', <h2>How can we help?</h2>)}
    {view.conversation.messages.map(message => {
     const Message = slots.message; const children = <p>{message.content}</p>;
     return <article key={message.id}>{Message ? <Message message={message}>{children}</Message> : children}</article>;
    })}
    {wrap('composer', <form onSubmit={e => { e.preventDefault(); send(); }}>
     <label>Preview message<textarea value={view.draft} onChange={e => actions.setDraft(e.target.value)} /></label>
     <button disabled={view.conversation.sending}>Send preview</button>
     <button type="button" onClick={actions.cancel} disabled={!view.conversation.sending}>Cancel preview</button>
     <label>Preview attachment<input type="file" onChange={e => { actions.attachFiles(Array.from(e.target.files || [])); e.target.value = ''; }} /></label>
     {view.attachments.map(f => <p key={f.index}>{f.name}<button type="button" onClick={() => actions.removeFile(f.index)}>Remove {f.name}</button></p>)}
    </form>)}
   </>)}</section>
  </div>
  <details><summary>Preview scenarios</summary>
   <button onClick={() => patch(v => ({ ...v, confirmations: [{ id: 'sample-confirm', operation: 'write', path: '/sample.txt', message: 'Preview file confirmation' }] }))}>Show confirmation</button>
   <button onClick={() => patch(v => ({ ...v, questions: [{ requestId: 'sample-question', questions: [{ id: 'q1', question: 'Preview question?', multiSelect: false, options: [{ id: 'yes', label: 'Yes' }] }] }] }))}>Show question</button>
   {view.confirmations.map(c => <p key={c.id}>{c.message}<button onClick={() => void actions.confirm(c.id, 'allow')}>Allow preview</button></p>)}
   {view.questions.map(q => <p key={q.requestId}>{q.questions[0].question}<button onClick={() => void actions.answer(q.requestId, [{ id: 'q1', selected: ['yes'] }])}>Answer preview</button></p>)}
  </details>
 </div></ChatRuntimeProvider>;
}
