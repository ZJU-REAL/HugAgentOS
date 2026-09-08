/**
 * 「当前打开的是哪个项目」与 catalogStore 的 activePanel 是同一份视图状态：
 * 面板恢复到 project_detail 时必须能拿回项目 id，否则主区域没东西可渲染。
 * 与面板一样按标签页存 sessionStorage，多窗口互不串台。
 */
const ACTIVE_PROJECT_STORAGE_KEY = 'hugagent_active_project';

export function loadActiveProjectId(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage.getItem(ACTIVE_PROJECT_STORAGE_KEY) || null;
  } catch {
    return null;
  }
}

export function saveActiveProjectId(projectId: string | null): void {
  if (typeof window === 'undefined') return;
  try {
    if (projectId) window.sessionStorage.setItem(ACTIVE_PROJECT_STORAGE_KEY, projectId);
    else window.sessionStorage.removeItem(ACTIVE_PROJECT_STORAGE_KEY);
  } catch {
    /* sessionStorage 不可用 */
  }
}
