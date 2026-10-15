import { create } from 'zustand';

export type TaskStatus = 'running' | 'completed' | 'waiting' | 'failed' | 'idle';
export interface TaskNotice {
  key: string; id: string; status: TaskStatus; startedAt: number; updatedAt: number;
  messageId?: string; seen: boolean;
}
interface Snapshot { notices: Record<string, TaskNotice>; personalReadSeq: number; historyReadBefore: number; personalBaselineReady: boolean }
interface TaskNotifications extends Snapshot {
  scope: string; personalUnread: number; personalReplying: boolean;
  hydrate: (scope: string, historyReadBefore?: number) => void;
  establishPersonalBaseline: (seq: number) => void;
  observe: (items: Omit<TaskNotice, 'seen'>[]) => void;
  markSeen: (key: string, id: string) => void;
  readPersonal: (seq: number) => void;
  setPersonal: (count: number, replying: boolean) => void;
}
const empty = (): Snapshot => ({ notices: {}, personalReadSeq: 0, historyReadBefore: 0, personalBaselineReady: false });
const storageKey = (scope: string) => 'hugagent.task-reminders.v1:' + scope;
const MAX_NOTICES = 2000;
function retained(notices:Record<string,TaskNotice>):Record<string,TaskNotice> {
  const priority=(n:TaskNotice)=>n.status==='waiting'?4:n.status==='running'?3:!n.seen?2:1;
  return Object.fromEntries(Object.entries(notices).sort((a,b)=>
    priority(b[1])-priority(a[1]) || b[1].updatedAt-a[1].updatedAt).slice(0,MAX_NOTICES));
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
          Number.isFinite(n.startedAt) && Number.isFinite(n.updatedAt)) notices[key] = {...n,seen:n.seen || (['completed','failed'].includes(n.status) && n.updatedAt <= historyReadBefore)};
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
    set({...empty(), ...(scope ? load(scope, cutoff) : {}), scope, personalUnread:0, personalReplying:false});
    persist(get()); // Save the one-time history baseline before any poll can arrive.
  },
  establishPersonalBaseline: seq => {
    if (get().personalBaselineReady || !Number.isSafeInteger(seq) || seq < 0) return;
    get().readPersonal(seq);
    set({personalBaselineReady:true}); persist(get());
  },
  observe: items => {
    const next = {...get().notices}; let changed = false;
    for (const item of items) {
      if (!Number.isFinite(item.startedAt) || !Number.isFinite(item.updatedAt)) continue;
      const previous = next[item.key];
      if (previous && (previous.startedAt > item.startedAt ||
          (previous.startedAt === item.startedAt && previous.updatedAt > item.updatedAt))) continue;
      if (previous && previous.id === item.id && previous.status === item.status && previous.updatedAt === item.updatedAt && previous.messageId === item.messageId) continue;
      next[item.key] = {...item, seen:(['completed','failed'].includes(item.status) && item.updatedAt <= get().historyReadBefore) || (previous?.id === item.id && previous.status === item.status ? previous.seen : false)};
      changed = true;
    }
    if (changed) { set({notices:retained(next)}); persist(get()); }
  },
  markSeen: (key,id) => {
    const n=get().notices[key];
    if (!n || n.id !== id || n.seen || !['completed','failed'].includes(n.status)) return;
    set({notices:{...get().notices,[key]:{...n,seen:true}}}); persist(get());
  },
  readPersonal: seq => {
    if (!Number.isSafeInteger(seq) || seq < 0) return;
    const state=get(); const notices={...state.notices}; let changed=false;
    for (const [key,n] of Object.entries(notices)) {
      if (key.startsWith('personal:') && !n.seen && ['completed','failed'].includes(n.status) &&
          Number(n.messageId)>0 && Number(n.messageId)<=seq) {
        notices[key]={...n,seen:true}; changed=true;
      }
    }
    const advanced=seq>state.personalReadSeq;
    if (!advanced && !changed) return;
    set({notices,personalReadSeq:Math.max(seq,state.personalReadSeq),
      personalUnread:advanced?0:state.personalUnread});
    persist(get());
  },
  setPersonal: (personalUnread,personalReplying) => set({personalUnread,personalReplying}),
}));

// Merge read acknowledgements across tabs. Old/in-flight responses cannot un-read a verdict.
if (typeof window !== 'undefined') window.addEventListener('storage', event => {
  const state=useTaskNotificationStore.getState();
  if (!state.scope || event.key !== storageKey(state.scope)) return;
  const saved=load(state.scope);
  state.observe(Object.values(saved.notices));
  for (const n of Object.values(saved.notices)) if (n.seen) state.markSeen(n.key,n.id);
  if (saved.personalReadSeq > state.personalReadSeq) state.readPersonal(saved.personalReadSeq);
});
