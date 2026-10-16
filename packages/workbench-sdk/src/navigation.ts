import type { Connection } from './index.js';
export interface WorkspaceRoute { path: string; requestId: string }
/** Navigation updates acknowledge the applied identity. Listeners render; they do not echo. */
export function workspaceNavigation(connection: Connection) {
  let sequence = 0;
  const identity = Array.from(crypto.getRandomValues(new Uint8Array(12)), value => value.toString(16).padStart(2,'0')).join('');
  return {
    get current(): WorkspaceRoute | undefined { return connection.info.route; },
    navigate(path: string, options: {replace?: boolean; signal?: AbortSignal} = {}) {
      return connection.request<WorkspaceRoute>('workspace.navigate', {
        path, replace: options.replace === true, requestId: 'navigation:' + identity + ':' + (++sequence),
      }, options.signal);
    },
    onChange(listener: (route: WorkspaceRoute) => void) {
      let applied = connection.info.route;
      return connection.onUpdate(info => {
        if (info.route && (applied?.requestId !== info.route.requestId || applied?.path !== info.route.path)) {
          applied = info.route; listener(info.route);
        }
      });
    },
  };
}
