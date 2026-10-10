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

/** Resolve a relative folder path under the linked root, never a same-named folder elsewhere. */
export async function prepareProjectFolderDeletion(project: ProjectItem, name: string): Promise<{
  fileCount: number;
  remove: () => Promise<void>;
}> {
  const rootId = project.linked_folder_id;
  if (!rootId) throw new Error(t('文件夹不存在'));
  const path = '/v1/myspace/folders';
  const response = await apiRequest<unknown>(path + '?as=tree');
  const root = findFolder(unwrapData<{ tree: FolderNode[] }>(response).tree || [], String(rootId));
  let folder = root;
  const parts = name.split('/');
  if (parts.some(part => !part || part === '.' || part === '..')) throw new Error(t('文件夹不存在'));
  for (const part of parts) {
    const matches = (folder?.children || []).filter(child => child.name === part);
    if (matches.length !== 1) throw new Error(t('文件夹不存在'));
    folder = matches[0];
  }
  const folderPath = path + '/' + encodeURIComponent(folder!.folder_id);
  const affected = await apiRequest<unknown>(folderPath + '/affected-count');
  return {
    fileCount: unwrapData<{ count: number }>(affected).count,
    remove: async () => { await apiRequest(folderPath, { method: 'DELETE' }); },
  };
}
