import { apiRequest, unwrapData } from '../../api';
import type { ProjectItem } from '../../types';
import { t } from '../../i18n';

interface FolderNode {
  folder_id: string;
  name: string;
  children?: FolderNode[];
}

/** Names from the tree root down to `id`, or null when the folder is not in the tree. */
function namesTo(nodes: FolderNode[], id: string): string[] | null {
  for (const node of nodes) {
    if (node.folder_id === id) return [node.name];
    const below = namesTo(node.children || [], id);
    if (below) return [node.name, ...below];
  }
  return null;
}

/**
 * Where a cloud project's folder lives in My Space: the space id and the path
 * inside it, matching the backend's project_location.
 */
export async function projectSpaceLocation(project: ProjectItem): Promise<{ space: string; path: string }> {
  const rootId = project.linked_folder_id;
  if (!rootId) throw new Error(t('项目未绑定文件目录'));
  const treePath = '/v1/myspace/folders';
  const response = await apiRequest<unknown>(treePath + '?as=tree');
  const names = namesTo(unwrapData<{ tree: FolderNode[] }>(response).tree || [], String(rootId));
  if (!names) throw new Error(t('项目文件夹不存在'));
  return { space: 'personal', path: names.join('/') };
}
