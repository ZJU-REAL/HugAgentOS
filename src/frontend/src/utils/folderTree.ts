/** Generic traversal utilities for the folder tree ("My Space" / teams). */

interface FolderTreeNode<T extends FolderTreeNode<T>> {
  folder_id: string;
  parent_folder_id: string | null;
  name: string;
  children?: T[];
}

/** Returns the list of direct child folders of a node; folderId=null means the root. */
export function childrenOfFolder<T extends FolderTreeNode<T>>(
  tree: T[],
  folderId: string | null,
): T[] {
  if (folderId === null) return tree;
  const found = findFolderById(tree, folderId);
  return found?.children ?? [];
}

/** Locate a node in the tree by folder_id (depth-first). */
export function findFolderById<T extends FolderTreeNode<T>>(
  tree: T[],
  folderId: string,
): T | null {
  const stack = [...tree];
  while (stack.length > 0) {
    const node = stack.pop()!;
    if (node.folder_id === folderId) return node;
    for (const c of node.children ?? []) stack.push(c);
  }
  return null;
}
