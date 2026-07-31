import { create } from 'zustand';

import { setHybridDual } from '../api';

/**
 * Desktop deployment-mode signal (module B/C). Probes the shell-only sentinel
 * ``/__desktop/setup/status``; on the web the endpoint 404s and ``isDesktop``
 * stays false. Used to hide the cloud-only "我的空间" (My Space) entry when
 * running against the local backend — locally there is no My Space; it is a
 * server concept, shown only when connected to the cloud (module C).
 *
 * Mode changes restart the app (activate-local / connect-server), so a one-shot
 * fetch per launch is enough — no live updates needed.
 */
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
  refresh: () => void;
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

export const useDeploymentModeStore = create<DeploymentModeState>((set, get) => ({
  isDesktop: false,
  activeLocal: false,
  provisionMode: '',
  serverBase: '',
  localBase: '',
  loaded: false,
  refresh: () => {
    if (get().loaded) return; // one-shot per launch
    fetch('/__desktop/setup/status', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('not desktop'))))
      .then((s: {
        active_local?: boolean;
        provision_mode?: string;
        current_server_base?: string;
        local_server_base?: string;
      }) => {
        // 混合路由开关：双模式下 api.ts 开始按项目/会话打 x-hugagent-target 头。
        setHybridDual(s.provision_mode === 'dual');
        set({
          isDesktop: true,
          activeLocal: !!s.active_local,
          provisionMode: s.provision_mode || '',
          serverBase: (s.current_server_base || '').replace(/\/+$/, ''),
          localBase: (s.local_server_base || '').replace(/\/+$/, ''),
          loaded: true,
        });
      })
      .catch(() =>
        set({
          isDesktop: false,
          activeLocal: false,
          provisionMode: '',
          serverBase: '',
          localBase: '',
          loaded: true,
        }),
      );
  },
}));

/**
 * 站点等对外链接的稳定源（展示 / 复制用，站内打开仍走相对路径）。
 *
 * 桌面窗口的 origin 是随机端口的本地反代——每次启动都变、仅本机可达，写进可
 * 分享链接必然失效。按站点归属选真实后端地址：
 *   - 本机站点 → 固定的 http://127.0.0.1:32101；
 *   - 云端站点 → 壳指向的云端地址（LocalOnly 形态下即本机地址），别的浏览器/
 *     别人打开都有效；
 *   - web 端 → 页面 origin（本来就是真实后端域名）。
 */
export function stablePublicOrigin(siteOrigin?: 'cloud' | 'local'): string {
  const { isDesktop, serverBase, localBase } = useDeploymentModeStore.getState();
  if (!isDesktop) return window.location.origin;
  if (siteOrigin === 'local' && localBase) return localBase;
  return serverBase || window.location.origin;
}
