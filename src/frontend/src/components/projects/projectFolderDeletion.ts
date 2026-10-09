import { apiRequest, unwrapData } from '../../api';
import type { ProjectItem } from '../../types';
import { t } from '../../i18n';

interface FolderNode {
  folder_id: string;
  name: string;
  children?: FolderNode[];
}

function findFolder(nodes: FolderNode[], id: string): FolderNode | undefined {
  for (const node of nodes) {
    if (node.folder_id === id) return node;
    const found = findFolder(node.children || [], id);
    if (found) return found;
  }
}

/** Resolve a direct child of the linked root, never a same-named folder elsewhere. */
export async function prepareProjectFolderDeletion(project: ProjectItem, name: string): Promise<{
  fileCount: number;
  remove: () => Promise<void>;
}> {
  const rootId = project.linked_folder_id;
  if (!rootId) throw new Error(t('文件夹不存在'));
  const path = '/v1/myspace/folders';
  const response = await apiRequest<unknown>(path + '?as=tree');
  const root = findFolder(unwrapData<{ tree: FolderNode[] }>(response).tree || [], String(rootId));
  const matches = (root?.children || []).filter(folder => folder.name === name);
  if (matches.length !== 1) throw new Error(t('文件夹不存在'));
  const folderPath = path + '/' + encodeURIComponent(matches[0].folder_id);
  const affected = await apiRequest<unknown>(folderPath + '/affected-count');
  return {
    fileCount: unwrapData<{ count: number }>(affected).count,
    remove: async () => { await apiRequest(folderPath, { method: 'DELETE' }); },
  };
}
