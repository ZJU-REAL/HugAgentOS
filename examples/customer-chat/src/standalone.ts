import './login.js';
import { Controls } from './controls.js';
import { ChatClient, type ChatEvent, type ChatHistoryMessage } from '@hugagent/chat-sdk';
import './standalone.css';

// Serve this independent application behind the platform's same-origin API gateway.
const client = new ChatClient({ baseUrl: import.meta.env.VITE_CUSTOMER_API_BASE || '/api' });
const element = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const status = element('status');
element<HTMLAnchorElement>('login').href = import.meta.env.VITE_CUSTOMER_LOGIN_URL || '/login?redirect=/session.html';
const input = element<HTMLTextAreaElement>('message');
const send = element<HTMLButtonElement>('send');
const cancel = element<HTMLButtonElement>('cancel');
const resume = element<HTMLButtonElement>('resume');
const messages = element('messages');
const controls = new Controls(client, element('controls'), () => chatId, text => { status.textContent = text; });
let chatId = new URL(location.href).searchParams.get('chat') || '';
let runId = '', offset = 0, assistant: HTMLElement | null = null;
let running = false;
let attachment: { file_id: string; name: string; mime_type: string; download_url: string } | null = null;
let controller: AbortController | undefined;
function row(role: string, content: string) {
  const item = document.createElement('p'); item.dataset.role = role; item.textContent = content;
  messages.append(item); return item;
}
function busy(value: boolean) {
  running = value; send.disabled = value; cancel.disabled = !value || !runId;
  element<HTMLButtonElement>('new').disabled = value;
  element<HTMLInputElement>('file').disabled = value;
  resume.disabled = value || !runId;
}
function rememberChat(id: string) {
  chatId = id; const url = new URL(location.href); url.searchParams.set('chat', id); history.replaceState(null, '', url);
}
async function historyMessages() {
  if (!chatId) return;
  const historyChatId = chatId;
  const history = { items: [] as ChatHistoryMessage[] };
  for (let page = 1; page <= 10000; page++) {
    const batch = await client.messages(historyChatId, page, 100);
    history.items.push(...batch.items);
    if (batch.items.length < 100) break;
    if (page === 10000) throw new Error('History limit reached');
  }
  if (chatId !== historyChatId) return;
  messages.replaceChildren();
  [...history.items].sort((a: ChatHistoryMessage, b: ChatHistoryMessage) => (a.chat_seq || 0) - (b.chat_seq || 0))
    .forEach(item => row(item.role, item.content));
}
function consume(event: ChatEvent) {
  controls.consume(event);
  if (event.type === 'run_snapshot' && event.state && typeof event.state === 'object') {
    const snapshot = event.state as Record<string, unknown>;
    const blocks = Array.isArray(snapshot.blocks) ? snapshot.blocks as Record<string, unknown>[] : [];
    let content = '';
    for (const block of blocks) {
      if (block.kind === 'answer') content = String(block.content || '');
      else if (block.kind === 'text') content += String(block.content || '');
    }
    assistant ??= messages.lastElementChild?.getAttribute('data-role') === 'assistant'
      ? messages.lastElementChild as HTMLElement : row('assistant', '');
    assistant.textContent = content;
    const signals = snapshot.signals && typeof snapshot.signals === 'object' ? Object.values(snapshot.signals) : [];
    for (const signal of signals) controls.consume(signal as ChatEvent);
  }
  if (event.type === 'run_started') { runId = String(event.run_id); cancel.disabled = false; }
  if (typeof event.event_offset === 'number') {
    if (event.event_offset <= offset) return;
    offset = event.event_offset;
  }
  if (event.type === 'content' || event.type === 'ai_message') {
    assistant ??= row('assistant', '');
    assistant.textContent += String(event.delta || event.content || '');
  }
  if (event.type === 'content_replace') {
    assistant ??= row('assistant', ''); assistant.textContent = String(event.content || event.text || '');
  }
  if (event.type === 'error') throw new Error(String(event.message || event.error || 'Run failed'));
}
async function follow(events: AsyncIterable<ChatEvent>) {
  busy(true); status.textContent = 'Working';
  try {
    for await (const event of events) consume(event);
    status.textContent = 'Complete'; runId = ''; await historyMessages();
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') { status.textContent = 'Cancelled'; runId = ''; }
    else status.textContent = String(error);
  }
  finally { busy(false); }
}
element<HTMLFormElement>('chat-form').addEventListener('submit', event => {
  event.preventDefault(); if (running) return;
  const text = input.value.trim(); if (!text) return;
  void (async () => {
    busy(true);
    try {
      if (!chatId) rememberChat((await client.createChat()).chat_id);
      input.value = ''; row('user', text); assistant = null; offset = 0; controller = new AbortController();
      const files = attachment ? [attachment] : [];
      attachment = null; element('attachment').textContent = '';
      await follow(client.send({ chat_id: chatId, message: text, ...(files.length ? { attachments: files } : {}) }, controller.signal));
    } catch (error) { status.textContent = String(error); busy(false); }
  })();
});
cancel.addEventListener('click', () => {
  void (async () => {
    try { await client.cancel(runId); controller?.abort(); status.textContent = 'Cancelled'; }
    catch (error) { status.textContent = String(error); }
  })();
});
resume.addEventListener('click', () => { controller = new AbortController(); void follow(client.resume(runId, offset, controller.signal)); });
element('new').addEventListener('click', () => {
  if (running) return; rememberChat(''); runId = ''; offset = 0; attachment = null;
  element('attachment').textContent = ''; input.value = '';
  (element<HTMLInputElement>('file')).value = '';
  messages.replaceChildren(); controls.clear(); assistant = null; status.textContent = 'Ready'; busy(false);
});
element<HTMLInputElement>('file').addEventListener('change', event => {
  const file = (event.target as HTMLInputElement).files?.[0]; if (!file || running) return;
  busy(true);
  void (async () => {
    try {
      if (!chatId) rememberChat((await client.createChat()).chat_id);
      attachment = await client.upload(file, chatId); element('attachment').textContent = attachment.name;
    } catch (error) { status.textContent = String(error); }
    finally { busy(false); }
  })();
});
void (async () => {
  busy(true);
  try {
    await historyMessages();
    if (!chatId) return;
    await controls.recover();
    const active = await client.activeRun(chatId);
    if (active?.run_id && !['completed', 'failed', 'cancelled'].includes(String(active.status))) {
      runId = String(active.run_id); controller = new AbortController();
      await follow(client.subscribe(runId, controller.signal));
    }
  } catch (error) { status.textContent = String(error); }
  finally { busy(false); }
})();
