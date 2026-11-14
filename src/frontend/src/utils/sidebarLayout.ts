import { apiRequest } from '../api';
import { desktopMenuActionUrl } from '../desktop/desktopBridge';
import { t } from '../i18n';
export type SidebarLayout = 'classic' | 'modern';

/** Name and one-line description of each layout, shared by Settings and the Config editor. */
export function sidebarLayoutText(layout: SidebarLayout): { label: string; description: string } {
  return layout === 'classic'
    ? { label: t('经典模式'), description: t('文字导航、项目与历史对话集中在同一侧栏') }
    : { label: t('现代模式'), description: t('图标导航与模块侧栏分开显示') };
}

export async function getSidebarLayout(userId: string): Promise<SidebarLayout> {
  const response = await apiRequest<{ data: { sidebar_layout?: unknown } }>(
    `/v1/users/${encodeURIComponent(userId)}/preferences`,
  );
  return response.data.sidebar_layout === 'classic' ? 'classic' : 'modern';
}

/** Save to the authenticated user's database before refreshing or restarting. */
export async function applySidebarLayout(userId: string, layout: SidebarLayout): Promise<void> {
  await apiRequest(`/v1/users/${encodeURIComponent(userId)}/preferences`, {
    method: 'PUT', body: JSON.stringify({ sidebar_layout: layout }),
  });
  if (window.__HG_DESKTOP__) window.location.href = desktopMenuActionUrl('sidebar_layout_apply');
  else window.location.reload();
}
