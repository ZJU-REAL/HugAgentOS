import { create } from 'zustand';

export type TaskStatus = 'running' | 'completed' | 'waiting' | 'failed' | 'idle';
export interface TaskNotice {
  key: string; id: string; status: TaskStatus; startedAt: number; updatedAt: number;
  messageId?: string; seen: boolean;
  /** Which backend owns the chat in desktop dual mode (undefined = cloud / single backend). */
  target?: 'local';
}
/** A server snapshot of one task. `read` is the server's read state for its result:
 *  undefined means the server does not track read state (an older backend). */
export type ObservedTask = Omit<TaskNotice, 'seen'> & { read?: boolean };
/** Pushes read acknowledgements to the backend that owns them (wired by useTaskReminderScope). */
export interface ReadSync {
  chat: (chatId: string, messageId: string, target?: 'local') => Promise<unknown>;
  personal: (seq: number) => Promise<{ read_seq: number }>;
}
// historyReadBefore is only a fallback for backends that do not report read state:
// the server read position is the source of truth wherever it exists.
interface Snapshot { notices: Record<string, TaskNotice>; personalReadSeq: number; historyReadBefore: number; personalBaselineReady: boolean }
interface TaskNotifications extends Snapshot {
  scope: string; personalUnread: number; personalReplying: boolean;
  hydrate: (scope: string, historyReadBefore?: number) => void;
  establishPersonalBaseline: (seq: number) => void;
  observe: (items: ObservedTask[]) => void;
  /** Acknowledge a result locally (optimistic) and, unless `sync` is false, on its server. */
  markSeen: (key: string, id: string, sync?: boolean) => void;
  readPersonal: (seq: number, sync?: boolean) => void;
  /** Merge the server's Personal Agent read position; a newer local one is pushed up. */
  applyPersonalServerRead: (seq: number) => void;
  setPersonal: (count: number, replying: boolean) => void;
}
const empty = (): Snapshot => ({ notices: {}, personalReadSeq: 0, historyReadBefore: 0, personalBaselineReady: false });
const storageKey = (scope: string) => 'hugagent.task-reminders.v1:' + scope;
const MAX_NOTICES = 2000;
const terminal = (status: TaskStatus) => status === 'completed' || status === 'failed';
function retained(notices:Record<string,TaskNotice>):Record<string,TaskNotice> {
  const priority=(n:TaskNotice)=>n.status==='waiting'?4:n.status==='running'?3:!n.seen?2:1;
  return Object.fromEntries(Object.entries(notices).sort((a,b)=>
    priority(b[1])-priority(a[1]) || b[1].updatedAt-a[1].updatedAt).slice(0,MAX_NOTICES));
}

let readSync: ReadSync | null = null;
export function configureReadSync(sync: ReadSync | null) { readSync = sync; }
// One request per acknowledgement at a time. A failed one is retried by later polls, a few times:
// a backend that keeps refusing it (an older one, a deleted message) is not asked forever.
const syncing = new Set<string>();
const failures = new Map<string, number>();
const MAX_READ_ATTEMPTS = 3;
// The highest Personal Agent position the server confirmed for the current scope.
let personalServerSeq = 0;
function pushChatRead(scope: string, n: TaskNotice) {
  if (!readSync || !n.key.startsWith('chat:') || !n.messageId || !terminal(n.status)) return;
  const token = scope + '\n' + n.key + '\n' + n.messageId;
  if (syncing.has(token) || (failures.get(token) || 0) >= MAX_READ_ATTEMPTS) return;
  syncing.add(token);
  readSync.chat(n.key.slice(5), n.messageId, n.target)
    .catch(() => { failures.set(token, (failures.get(token) || 0) + 1); /* The local acknowledgement stays. */ })
    .finally(() => syncing.delete(token));
}
function pushPersonalRead(scope: string, seq: number) {
  if (!readSync || seq <= personalServerSeq) return;
  const token = scope + '\npersonal\n' + seq;
  if (syncing.has(token)) return;
  syncing.add(token);
  readSync.personal(seq)
    .then(result => {
      if (useTaskNotificationStore.getState().scope === scope && Number.isSafeInteger(result?.read_seq))
        personalServerSeq = Math.max(personalServerSeq, result.read_seq);
    })
    .catch(() => { /* Retried on the next read or status poll. */ })
    .finally(() => syncing.delete(token));
}

function load(scope: string, fallbackCutoff = Date.now()): Snapshot {
  try {
    const data = JSON.parse(localStorage.getItem(storageKey(scope)) || '{}');
    const historyReadBefore = Number.isFinite(data.historyReadBefore) && data.historyReadBefore >= 0 ? data.historyReadBefore : fallbackCutoff;
    const notices: Record<string, TaskNotice> = {};
    for (const [key, value] of Object.entries(data.notices || {})) {
      const n = value as TaskNotice;
      if (n && n.key === key && typeof n.id === 'string' &&
          ['running', 'completed', 'waiting', 'failed', 'idle'].includes(n.status) &&
          Number.isFinite(n.startedAt) && Number.isFinite(n.updatedAt)) notices[key] = {...n,seen:n.seen || (terminal(n.status) && n.updatedAt <= historyReadBefore)};
    }
    return { historyReadBefore, personalBaselineReady: data.personalBaselineReady === true, notices:retained(notices), personalReadSeq: Number.isSafeInteger(data.personalReadSeq) ? Math.max(0, data.personalReadSeq) : 0 };
  } catch { return {...empty(),historyReadBefore:fallbackCutoff}; }
}
function persist(s: TaskNotifications) {
  if (!s.scope) return;
  try { localStorage.setItem(storageKey(s.scope), JSON.stringify({
    notices: s.notices,
    personalReadSeq: s.personalReadSeq,
    historyReadBefore: s.historyReadBefore, personalBaselineReady: s.personalBaselineReady,
  })); } catch { /* Private browsing or quota: reminders remain usable in memory. */ }
}
export function normalizeTaskStatus(status: string): TaskStatus {
  if (['pending','queued','running'].includes(status)) return 'running';
  if (['waiting','needs_attention'].includes(status)) return 'waiting';
  if (status === 'completed') return 'completed';
  if (status === 'failed') return 'failed';
  return 'idle'; // Cancellation is never a successful completion.
}
export function taskIndicator(notices: TaskNotice[], running = false, waiting = false): { status: TaskStatus; seen: boolean } {
  if (waiting || notices.some(n => n.status === 'waiting')) return {status:'waiting',seen:false};
  if (notices.some(n => n.status === 'failed' && !n.seen)) return {status:'failed',seen:false};
  if (running || notices.some(n => n.status === 'running')) return {status:'running',seen:false};
  if (notices.some(n => n.status === 'completed' && !n.seen)) return {status:'completed',seen:false};
  if (notices.some(n => n.status === 'failed')) return {status:'failed',seen:true};
  if (notices.some(n => n.status === 'completed')) return {status:'completed',seen:true};
  return {status:'idle',seen:true};
}
export const useTaskNotificationStore = create<TaskNotifications>((set, get) => ({
  ...empty(), scope:'', personalUnread:0, personalReplying:false,
  hydrate: (scope, cutoff = Date.now()) => {
    if (scope === get().scope) return;
    personalServerSeq = 0;
    failures.clear();
    set({...empty(), ...(scope ? load(scope, cutoff) : {}), scope, personalUnread:0, personalReplying:false});
    persist(get()); // Save the fallback baseline before any poll can arrive.
  },
  establishPersonalBaseline: seq => {
    // Only for a backend without a server read position (see applyPersonalServerRead).
    if (get().personalBaselineReady || !Number.isSafeInteger(seq) || seq < 0) return;
    get().readPersonal(seq, false);
    set({personalBaselineReady:true}); persist(get());
  },
  observe: items => {
    const state = get(); const next = {...state.notices}; let changed = false;
    const retry: TaskNotice[] = [];
    for (const item of items) {
      if (!Number.isFinite(item.startedAt) || !Number.isFinite(item.updatedAt)) continue;
      const previous = next[item.key];
      if (previous && (previous.startedAt > item.startedAt ||
          (previous.startedAt === item.startedAt && previous.updatedAt > item.updatedAt))) continue;
      const { read, ...task } = item;
      const sameVerdict = previous?.id === item.id && previous.status === item.status;
      // A local acknowledgement is optimistic: a server that has not caught up yet
      // (in-flight or failed mark-read) cannot undo it, and gets the mark again.
      const localSeen = sameVerdict ? previous.seen : false;
      const legacySeen = read === undefined && terminal(item.status) && item.updatedAt <= state.historyReadBefore;
      const seen = read === true || localSeen || legacySeen;
      if (read === false && localSeen && terminal(item.status)) retry.push({...task, seen});
      if (previous && sameVerdict && previous.updatedAt === item.updatedAt && previous.messageId === item.messageId &&
          previous.seen === seen && previous.target === item.target) continue;
      next[item.key] = {...task, seen};
      changed = true;
    }
    if (changed) { set({notices:retained(next)}); persist(get()); }
    for (const n of retry) pushChatRead(state.scope, n);
  },
  markSeen: (key, id, sync = true) => {
    const n=get().notices[key];
    if (!n || n.id !== id || n.seen || !terminal(n.status)) return;
    const seen={...n,seen:true};
    set({notices:{...get().notices,[key]:seen}}); persist(get());
    if (sync) pushChatRead(get().scope, seen);
  },
  readPersonal: (seq, sync = true) => {
    if (!Number.isSafeInteger(seq) || seq < 0) return;
    const state=get(); const notices={...state.notices}; let changed=false;
    for (const [key,n] of Object.entries(notices)) {
      if (key.startsWith('personal:') && !n.seen && terminal(n.status) &&
          Number(n.messageId)>0 && Number(n.messageId)<=seq) {
        notices[key]={...n,seen:true}; changed=true;
      }
    }
    if (sync) pushPersonalRead(state.scope, seq);
    const advanced=seq>state.personalReadSeq;
    if (!advanced && !changed) return;
    set({notices,personalReadSeq:Math.max(seq,state.personalReadSeq),
      personalUnread:advanced?0:state.personalUnread});
    persist(get());
  },
  applyPersonalServerRead: seq => {
    if (!Number.isSafeInteger(seq) || seq < 0) return;
    personalServerSeq = Math.max(personalServerSeq, seq);
    // The server decides history: no first-load baseline is needed with it.
    if (!get().personalBaselineReady) { set({personalBaselineReady:true}); persist(get()); }
    get().readPersonal(seq, false);
    // Read here while offline, or before this backend tracked reads: tell it now.
    if (get().personalReadSeq > personalServerSeq) pushPersonalRead(get().scope, get().personalReadSeq);
  },
  setPersonal: (personalUnread,personalReplying) => set({personalUnread,personalReplying}),
}));

// Merge read acknowledgements across tabs. Old/in-flight responses cannot un-read a verdict.
// The tab that acknowledged already told the server, so merging here does not sync again.
if (typeof window !== 'undefined') window.addEventListener('storage', event => {
  const state=useTaskNotificationStore.getState();
  if (!state.scope || event.key !== storageKey(state.scope)) return;
  const saved=load(state.scope);
  state.observe(Object.values(saved.notices));
  for (const n of Object.values(saved.notices)) if (n.seen) state.markSeen(n.key,n.id,false);
  if (saved.personalReadSeq > state.personalReadSeq) state.readPersonal(saved.personalReadSeq,false);
});
