import { useEffect } from 'react';
import { create } from 'zustand';
import { workbenchRequest as apiRequest, preferenceSource } from './sourceRequest';
import { useAuthStore } from '../stores/authStore';
import { useDeploymentModeStore } from '../stores/deploymentModeStore';
import { ApiResponseError } from '../utils/apiError';
import { t } from '../i18n';

export interface NavigationPreferences { revision: number; pinned: string[] | null; order: string[]; environment: string }
interface State { owner: string; value: NavigationPreferences; loading: boolean; saving: boolean; error: string }
function validated(value: NavigationPreferences, environment: string) {
  const keys = (candidate: unknown) => Array.isArray(candidate) && candidate.length <= 200
    && candidate.every(key => typeof key === 'string') && new Set(candidate).size === candidate.length;
  if (!value || !Number.isInteger(value.revision) || value.revision < 0 || value.environment !== environment
    || !(value.pinned === null || keys(value.pinned)) || !keys(value.order)) throw new Error('Invalid navigation preferences');
  return value;
}
const initial = (environment: string): NavigationPreferences => ({ revision: 0, pinned: null, order: [], environment });
const store = create<State>(() => ({ owner: '', value: initial('web'), loading: true, saving: false, error: '' }));
let readRequest: AbortController | undefined;
let writeRequest: AbortController | undefined;
let generation = 0;
let consumers = 0;
let timer: ReturnType<typeof setInterval> | undefined;
function context() {
  const mode = useDeploymentModeStore.getState().provisionMode;
  const environment = mode === 'local_only' ? 'local' : mode === 'cloud_only' ? 'cloud' : mode === 'dual' ? 'dual' : 'web';
  return { environment, owner: JSON.stringify([useAuthStore.getState().authUser?.user_id, environment, useDeploymentModeStore.getState().serverBase]) };
}
function synchronize() {
  const current = context();
  if (store.getState().owner !== current.owner) {
    generation++; readRequest?.abort(); writeRequest?.abort();
    store.setState({ owner: current.owner, value: initial(current.environment), loading: true, saving: false, error: '' });
  }
  return current;
}
const path = (environment: string) => '/v1/workbench/me/navigation-preferences?environment=' + environment;
export async function reloadNavigationPreferences() {
  const current = synchronize();
  if (store.getState().saving) return;
  readRequest?.abort(); const controller = new AbortController(); readRequest = controller;
  const version = generation;
  try {
    const response = await apiRequest<{ data: NavigationPreferences }>(path(current.environment), { signal: controller.signal }, preferenceSource());
    if (!controller.signal.aborted && version === generation && context().owner === current.owner)
      store.setState({ value: validated(response.data, current.environment), loading: false, error: '' });
  } catch {
    if (!controller.signal.aborted && version === generation && context().owner === current.owner)
      store.setState({ loading: false, error: t('导航偏好加载失败，请重试') });
  }
}
export async function saveNavigationPreferences(pinned: string[] | null, order: string[], expectedRevision?: number): Promise<{ saved: boolean; latest?: NavigationPreferences }> {
  const current = synchronize();
  if (store.getState().loading || store.getState().saving) return { saved: false };
  readRequest?.abort(); writeRequest?.abort();
  const controller = new AbortController(); writeRequest = controller;
  const version = generation; const revision = expectedRevision ?? store.getState().value.revision;
  store.setState({ saving: true, error: '' });
  try {
    const response = await apiRequest<{ data: NavigationPreferences }>(path(current.environment), {
      method: 'PUT', signal: controller.signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ revision, pinned, order }),
    }, preferenceSource());
    if (!controller.signal.aborted && version === generation && context().owner === current.owner) {
      store.setState({ value: validated(response.data, current.environment), saving: false, error: '' });
      return { saved: true };
    }
  } catch (error) {
    if (error instanceof ApiResponseError && error.status === 409 && !controller.signal.aborted) {
      try {
        const latest = await apiRequest<{ data: NavigationPreferences }>(path(current.environment), { signal: controller.signal }, preferenceSource());
        if (!controller.signal.aborted && version === generation && context().owner === current.owner) {
          store.setState({ value: validated(latest.data, current.environment), saving: false, error: t('导航偏好已在其他设备修改，已加载最新内容，请重新应用更改') });
          return { saved: false, latest: validated(latest.data, current.environment) };
        }
      } catch { /* Retain explicit conflict and retry when the latest read fails. */ }
    }
    if (!controller.signal.aborted && version === generation && context().owner === current.owner)
      store.setState({ saving: false, error: error instanceof ApiResponseError && error.status === 409
        ? t('导航偏好已在其他设备修改，请重新加载后重试') : t('导航偏好保存失败，请重试') });
  }
  return { saved: false };
}
export function useNavigationPreferences() {
  const userId = useAuthStore(s => s.authUser?.user_id);
  const mode = useDeploymentModeStore(s => s.provisionMode);
  const server = useDeploymentModeStore(s => s.serverBase);
  const state = store(); const current = context();
  useEffect(() => {
    const first = consumers++ === 0;
    if (first) { window.addEventListener('focus', reloadNavigationPreferences); timer = setInterval(reloadNavigationPreferences, 15000); }
    if (first || store.getState().owner !== context().owner) void reloadNavigationPreferences();
    return () => { if (--consumers === 0) { window.removeEventListener('focus', reloadNavigationPreferences); clearInterval(timer); generation++; readRequest?.abort(); writeRequest?.abort(); store.setState({ saving: false }); } };
  }, [userId, mode, server]);
  return state.owner === current.owner ? state : { owner: current.owner, value: initial(current.environment), loading: true, saving: false, error: '' };
}

export function orderedNavigation(defaults: string[], forced: string[], preferences: NavigationPreferences) {
  const selected = [...new Set([...forced, ...(preferences.pinned ?? defaults)])];
  const ranks = new Map(preferences.order.map((key, index) => [key, index]));
  return selected.map((key, index) => ({ key, index })).sort((a, b) =>
    (ranks.get(a.key) ?? preferences.order.length + a.index) - (ranks.get(b.key) ?? preferences.order.length + b.index)).map(item => item.key);
}
