import { apiRequest } from '../api';
import { desktopMenuActionUrl } from '../desktop/desktopBridge';
export type SidebarLayout = 'classic' | 'modern';

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
