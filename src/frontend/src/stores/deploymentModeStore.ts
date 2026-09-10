import { create } from 'zustand';

import { setHybridDual } from '../api';

/**
 * Desktop deployment-mode signal.
 *
 * The desktop shell injects ``window.__HG_DESKTOP__`` into the served
 * ``index.html``, so the running shape (provision mode, backend bases) is known
 * synchronously before any store or effect runs. On the web the global is
 * absent and ``isDesktop`` stays false. Used to hide the cloud-only "我的空间"
 * (My Space) entry when running against the local backend.
 *
 * In dual mode the shell also pushes readiness of the local execution plane
 * over ``/__desktop/events`` (SSE): ``localReady`` flips once the cloud identity
 * has been bridged to the local backend, i.e. once local-routed requests can
 * be served.
 */
interface DesktopBoot {
  provision_mode?: string;
  active_local?: boolean;
  server_base?: string;
  local_base?: string;
}

interface DesktopEvent {
  bridge?: { identity_ready?: boolean; capabilities_ready?: boolean; models_ready?: boolean; error?: string | null };
}

declare global {
  interface Window {
    __HG_DESKTOP__?: DesktopBoot;
  }
}

interface DeploymentModeState {
  isDesktop: boolean;
  activeLocal: boolean;
  /** 初始化选定的运行形态：'local_only' | 'cloud_only' | 'dual'（web 上为空串）。 */
  provisionMode: string;
  /** 壳当前指向的后端根地址（本机模式为 http://127.0.0.1:32101，云端/双模式为云端地址；web 上为空串）。 */
  serverBase: string;
  /** 本机后端固定基址（http://127.0.0.1:32101；web 上为空串）。 */
  localBase: string;
  loaded: boolean;
  /** 双模式：本机执行面已认出当前云端身份，本机路由可用。 */
  localReady: boolean;
  capabilitiesReady: boolean;
  modelsReady: boolean;
  capabilityGateOpen: boolean;
  partialCapabilities: boolean;
  capabilitySyncError: string | null;
}

export interface ProjectCreationTargets {
  cloud: boolean;
  local: boolean;
}

/**
 * Project creation follows the provisioned desktop capability set.
 * Unknown/legacy desktop modes stay cloud-only as the safe fallback; the web
 * application has no local-folder picker and is therefore cloud-only too.
 */
export function projectCreationTargets(
  isDesktop: boolean,
  provisionMode: string,
): ProjectCreationTargets {
  if (!isDesktop) return { cloud: true, local: false };
  if (provisionMode === 'local_only') return { cloud: false, local: true };
  if (provisionMode === 'dual') return { cloud: true, local: true };
  return { cloud: true, local: false };
}

function bootState(): DeploymentModeState {
  const boot = typeof window !== 'undefined' ? window.__HG_DESKTOP__ : undefined;
  if (!boot) {
    return {
      isDesktop: false,
      activeLocal: false,
      provisionMode: '',
      serverBase: '',
      localBase: '',
      loaded: true,
      localReady: false,
      capabilitiesReady: false,
      modelsReady: false,
      capabilityGateOpen: true,
      partialCapabilities: false,
      capabilitySyncError: null,
    };
  }
  return {
    isDesktop: true,
    activeLocal: !!boot.active_local,
    provisionMode: boot.provision_mode || '',
    serverBase: (boot.server_base || '').replace(/\/+$/, ''),
    localBase: (boot.local_base || '').replace(/\/+$/, ''),
    loaded: true,
    localReady: false,
    capabilitiesReady: false,
    modelsReady: false,
    capabilityGateOpen: true,
    partialCapabilities: false,
    capabilitySyncError: null,
  };
}

const initial = bootState();
// 混合路由开关：双模式下 api.ts 按项目/会话打 x-hugagent-target 头。
setHybridDual(initial.provisionMode === 'dual');

export const useDeploymentModeStore = create<DeploymentModeState>(() => initial);

if (initial.provisionMode === 'dual' && typeof EventSource !== 'undefined') {
  // EventSource 自带断线重连；每帧都是完整状态，丢帧无害。
  const events = new EventSource('/__desktop/events');
  events.onmessage = (message) => {
    try {
      const status = JSON.parse(message.data) as DesktopEvent;
      const localReady = !!status.bridge?.identity_ready;
      const capabilitiesReady = !!status.bridge?.capabilities_ready;
      const modelsReady = !!status.bridge?.models_ready;
      const capabilitySyncError = status.bridge?.error || null;
      const previous = useDeploymentModeStore.getState();
      if (localReady !== previous.localReady || capabilitiesReady !== previous.capabilitiesReady
          || modelsReady !== previous.modelsReady || capabilitySyncError !== previous.capabilitySyncError) {
        useDeploymentModeStore.setState({ localReady, capabilitiesReady, modelsReady, capabilitySyncError,
          ...(!localReady ? { capabilityGateOpen: true, partialCapabilities: false } : {}),
        });
      }
    } catch {
      /* 非 JSON 帧（心跳）忽略 */
    }
  };
}

/**
 * 站点等对外链接的稳定源：壳当前指向的后端地址。
 *
 * 桌面窗口的 origin 是随机端口的本地反代——每次启动都变、仅本机可达，任何相对
 * 路径都会解析成它，写进链接或另开标签都只有本机打得开。所以对外链接一律用这里
 * 的绝对地址：双模式为云端域名，LocalOnly 形态下 serverBase 本身即本机地址，
 * web 端就是页面 origin（本来就是真实后端域名）。
 */
export function stablePublicOrigin(): string {
  const { isDesktop, serverBase } = useDeploymentModeStore.getState();
  if (!isDesktop) return window.location.origin;
  return serverBase;
}
