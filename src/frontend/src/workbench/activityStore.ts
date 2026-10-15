import {sourceTargets} from './sourceIdentity';
import {identityKey} from './invalidation';
import { useEffect } from 'react';
import { create } from 'zustand';
import { useDeploymentModeStore } from '../stores/deploymentModeStore';
import type { RequestTarget } from '../api';
import { useAuthStore } from '../stores/authStore';
import { useApplicationDirectory } from './catalog';
import { activityRequest } from './activityApi';

export interface ActivityProjection {
  badges: Record<string, {count: number}>;
  notifications: {id: string; applicationKey: string; title: string; body: string; source?: RequestTarget}[];
  background: {resourceId: string; applicationKey: string; instanceId: string; expiresAt: number; source?: RequestTarget}[];
}
const empty = (): ActivityProjection => ({badges: {}, notifications: [], background: []});
const state = create<ActivityProjection & {userId?: string; error: boolean}>(() => ({...empty(), error: false}));
let request: AbortController | undefined;
let timer: ReturnType<typeof setInterval> | undefined;
let consumers = 0;
let owner: string | undefined;

export function reloadActivity() {
  const userId = useAuthStore.getState().authUser?.user_id;
  request?.abort();
  if (identityKey() !== owner) state.setState({...empty(), userId, error: false});
  owner = identityKey();
  if (!userId) return;
  const controller = new AbortController(); request = controller;
  void Promise.allSettled(sourceTargets(useDeploymentModeStore.getState()).map(async source => {
    const result = await activityRequest<ActivityProjection>('/v1/workbench/me/activity', 'GET',
      undefined, controller.signal, source);
    if (!result.badges || !Array.isArray(result.notifications) || !Array.isArray(result.background)) throw new Error();
    return {...result, notifications: result.notifications.map(n => ({...n, source})),
      background: result.background.map(b => ({...b, source}))};
  })).then(results => {
    if (controller.signal.aborted || owner !== identityKey()) return;
    const merged = empty();
    for (const result of results) if (result.status === 'fulfilled') {
      Object.assign(merged.badges, result.value.badges);
      merged.notifications.push(...result.value.notifications);
      merged.background.push(...result.value.background);
    }
    state.setState({...merged, userId, error: results.some(r => r.status === 'rejected')});
  });
}
export function useActivity() {
  const userId = useAuthStore(s => s.authUser?.user_id);
  const catalog = useApplicationDirectory();
  const mode = useDeploymentModeStore(s => s.provisionMode + ':' + s.localReady + ':' + s.isDesktop);
  const current = state();
  useEffect(() => {
    if (consumers++ === 0) {
      reloadActivity(); window.addEventListener('focus', reloadActivity);
      timer = setInterval(reloadActivity, 15000);
    } else if (owner !== identityKey()) reloadActivity();
    return () => {
      if (--consumers === 0) {
        request?.abort(); clearInterval(timer); window.removeEventListener('focus', reloadActivity);
      }
    };
  }, [userId, mode]);
  if (current.userId !== userId || catalog.userId !== userId || catalog.error) return {...empty(), error: current.error};
  const visible = new Set(catalog.items.filter(i => i.availability.available).map(i => i.applicationKey));
  return {
    badges: Object.fromEntries(Object.entries(current.badges).filter(([key]) => visible.has(key))),
    notifications: current.notifications.filter(n => visible.has(n.applicationKey)),
    background: current.background.filter(b => visible.has(b.applicationKey)),
    error: current.error,
  };
}
