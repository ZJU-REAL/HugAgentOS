import {randomUUID} from './uuid';
import { useAuthStore } from '../stores/authStore';
import { useDeploymentModeStore } from '../stores/deploymentModeStore';
import { getApiUrl } from '../api';

export type RevocationReason = 'identity' | 'logout' | 'directory' | 'mutation' | 'unauthorized';
export interface WorkbenchSession {
  identity: string;
  applicationKey: string;
  revision: string | number;
  revoke: (reason: RevocationReason) => void;
}
const sessions = new Set<WorkbenchSession>();
const listeners = new Set<(reason: RevocationReason) => void>();
let watched = false;
let previousIdentity = '';
let channel: BroadcastChannel | undefined;

let deviceId = '';
function browserDeviceId() {
  if (deviceId) return deviceId;
  try {
    deviceId = localStorage.getItem('hugagent-workbench-device') || randomUUID();
    localStorage.setItem('hugagent-workbench-device', deviceId);
  } catch { deviceId = randomUUID(); }
  return deviceId;
}
export function identityKey(): string {
  const auth = useAuthStore.getState();
  const mode = useDeploymentModeStore.getState();
  return JSON.stringify([auth.loggingOut ? null : auth.authUser?.user_id,
    mode.serverBase || getApiUrl(), mode.localBase, mode.provisionMode, mode.activeLocal,
    browserDeviceId()]);
}
function watchIdentity() {
  if (watched) return;
  watched = true;
  previousIdentity = identityKey();
  const changed = () => {
    const next = identityKey();
    if (next === previousIdentity) return;
    previousIdentity = next;
    revokeWorkbenchSessions(useAuthStore.getState().loggingOut ? 'logout' : 'identity');
    emit('identity');
  };
  useAuthStore.subscribe(changed);
  useDeploymentModeStore.subscribe(changed);
}
function emit(reason: RevocationReason) {
  for (const listener of [...listeners]) listener(reason);
}
export function registerWorkbenchSession(session: WorkbenchSession): () => void {
  watchIdentity();
  if (session.identity !== identityKey() || !useAuthStore.getState().authUser || useAuthStore.getState().loggingOut) {
    session.revoke('identity');
    return () => {};
  }
  sessions.add(session);
  return () => { sessions.delete(session); };
}
export function revokeWorkbenchSessions(reason: RevocationReason, applicationKey?: string) {
  const failures: unknown[] = [];
  for (const session of [...sessions]) {
    if (applicationKey && session.applicationKey !== applicationKey) continue;
    sessions.delete(session);
    try { session.revoke(reason); } catch (error) { failures.push(error); }
  }
  // A faulty application's cleanup must not keep other applications alive.
  if (failures.length) window.dispatchEvent(new CustomEvent('workbench:cleanup-failed',
    { detail: { count: failures.length } }));
}
export function reconcileWorkbenchSessions(items: {
  applicationKey: string; revision: number | string;
  availability: { available: boolean };
}[]) {
  const available = new Map(items.filter(item => item.availability.available)
    .map(item => [item.applicationKey, String(item.revision)]));
  for (const session of [...sessions]) {
    if (session.identity !== identityKey()
      || !available.has(session.applicationKey)
      || (!session.applicationKey.startsWith('plugin:') && available.get(session.applicationKey)!==String(session.revision))) {
      sessions.delete(session);
      try { session.revoke('directory'); } catch {
        window.dispatchEvent(new CustomEvent('workbench:cleanup-failed', { detail: { count: 1 } }));
      }
    }
  }
}
export function subscribeDirectoryInvalidation(listener: (reason: RevocationReason) => void) {
  watchIdentity();
  listeners.add(listener);
  if (!channel && typeof BroadcastChannel !== 'undefined') {
    channel = new BroadcastChannel('hugagent-workbench-directory-v1');
    channel.onmessage = event => {
      // Shared-browser hints only trigger an authenticated refetch; never grant/revoke from payload.
      if (event.data?.identity === identityKey() && event.data?.type === 'invalidate') emit('mutation');
    };
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) { channel?.close(); channel = undefined; }
  };
}
export function invalidateWorkbenchDirectory(reason: RevocationReason = 'mutation') {
  watchIdentity();
  emit(reason);
  // Do not send application names, auth tokens or projected directory contents.
  if (channel) channel.postMessage({ type: 'invalidate', identity: identityKey() });
  else if (typeof BroadcastChannel !== 'undefined') {
    const sender = new BroadcastChannel('hugagent-workbench-directory-v1');
    sender.postMessage({ type: 'invalidate', identity: identityKey() });
    sender.close();
  }
}


// Capture ownerScope once per WB10 pool; old loads cannot register in a new account.
export function createSessionRegistration(ownerScope = identityKey()) {
  return (entry: { identity: { userId: string; applicationKey: string; packageRevision: string } },
    revoke: () => void) => {
    if (entry.identity.userId !== useAuthStore.getState().authUser?.user_id) {
      revoke(); return () => {};
    }
    return registerWorkbenchSession({ identity: ownerScope,
      applicationKey: entry.identity.applicationKey, revision: entry.identity.packageRevision, revoke });
  };
}
