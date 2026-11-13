import { authFetch, getApiUrl } from '../../../api';
import { parseSSE } from '../../../utils/sse';

/**
 * What the server says about this person's Personal Agent. `ready` comes on every (re)connection, and readers then
 * load everything, so nothing changed while disconnected is missed; `down` means the connection dropped and is being
 * re-established. `changed` names topics: a conversation id when its messages changed, `operation:<id>` when a session
 * operation's receipt changed, `device:<id>` / `device:<id>:<chat>` when a linked computer's chat list or one of its
 * chats changed. Tasks, computers and settings change without a topic of their own.
 */
export type PersonalSignal = { type: 'ready' } | { type: 'changed'; topics: string[] } | { type: 'down' };
type Listener = (signal: PersonalSignal) => void;

const listeners = new Set<Listener>();
let controller: AbortController | null = null;
// While the tab is hidden, signals are merged and delivered when it is shown again.
let held: PersonalSignal | null = null;

function deliver(signal: PersonalSignal) {
  if (document.visibilityState !== 'visible' && signal.type !== 'down') {
    if (!held) held = signal;
    else if (held.type === 'changed' && signal.type === 'changed') held = { type: 'changed', topics: [...new Set([...held.topics, ...signal.topics])] };
    // A pending full reload covers any change.
    else held = { type: 'ready' };
    return;
  }
  listeners.forEach(listener => listener(signal));
}

function onVisible() {
  if (document.visibilityState !== 'visible' || !held) return;
  const signal = held;
  held = null;
  deliver(signal);
}

async function follow(signal: AbortSignal) {
  let failures = 0;
  while (!signal.aborted) {
    try {
      const response = await authFetch(getApiUrl() + '/v1/personal-agent/events', { signal, headers: { Accept: 'text/event-stream' } });
      if (!response.ok) throw new Error('Personal Agent events: ' + response.status);
      for await (const event of parseSSE<PersonalSignal>(response)) {
        if (event.type === 'ready') failures = 0;
        deliver(event);
      }
    } catch { /* reconnect below */ }
    if (signal.aborted) return;
    deliver({ type: 'down' });
    // Reconnect, sooner after a brief drop than after repeated failures.
    const delay = Math.min(1000 * 2 ** failures++, 30_000);
    await new Promise<void>(resolve => {
      const timer = setTimeout(done, delay);
      function done() { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); }
      signal.addEventListener('abort', done, { once: true });
    });
  }
}

/** Hear the Personal Agent's changes. One connection per tab serves every listener and closes with the last one. */
export function subscribePersonalChanges(listener: Listener): () => void {
  listeners.add(listener);
  if (!controller) {
    controller = new AbortController();
    document.addEventListener('visibilitychange', onVisible);
    void follow(controller.signal);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size || !controller) return;
    controller.abort();
    controller = null;
    held = null;
    document.removeEventListener('visibilitychange', onVisible);
  };
}

/**
 * Resolves when the server signals `topic`, or reconnects (what it waited for may have happened meanwhile), or after
 * `ms`. The caller then reads the state itself. Hidden tabs are told when shown again.
 */
export function waitForTopic(topic: string, ms: number, abort?: AbortSignal): Promise<void> {
  return new Promise(resolve => {
    const done = () => { clearTimeout(timer); unsubscribe(); abort?.removeEventListener('abort', done); resolve(); };
    const timer = setTimeout(done, ms);
    const unsubscribe = subscribePersonalChanges(signal => {
      if (signal.type === 'ready' || (signal.type === 'changed' && signal.topics.includes(topic))) done();
    });
    abort?.addEventListener('abort', done, { once: true });
  });
}
