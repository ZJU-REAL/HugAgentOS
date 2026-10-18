import { useState } from 'react';
import { Tree, message } from 'antd';
import type { DataNode } from 'antd/es/tree';
import { FolderOutlined } from '@ant-design/icons';
import { listSpaceEntries, type SpaceRoot } from './spaceFilesApi';

const keyFor = (space: string, path: string) => JSON.stringify([space, path]);
function replaceChildren(nodes: DataNode[], key: string, children: DataNode[]): DataNode[] {
  return nodes.map(node => node.key === key ? { ...node, children }
    : node.children ? { ...node, children: replaceChildren(node.children, key, children) } : node);
}
interface Props {
  roots: SpaceRoot[]; selected: string; path: string; revision: number;
  onSelect: (space: string, path: string) => void;
}
export function SpaceDirectoryTree(props: Props) {
  const identity = JSON.stringify(props.roots.map(root => [root.space_id, root.name]));
  return <DirectoryTree key={identity + props.revision} {...props} />;
}
function DirectoryTree({ roots, selected, path, onSelect }: Props) {
  const [nodes, setNodes] = useState<DataNode[]>(() => roots.map(root => ({
    key: keyFor(root.space_id, ''), title: root.name, icon: <FolderOutlined />, isLeaf: false,
  })));
  async function load(key: string) {
    const [space, path] = JSON.parse(key) as [string, string];
    const children: DataNode[] = [];
    let cursor: string | undefined;
    do {
      const page = await listSpaceEntries(space, path, cursor);
      children.push(...page.entries.filter(entry => entry.is_directory).map(entry => ({
        key: keyFor(space, entry.path), title: entry.name, icon: <FolderOutlined />, isLeaf: false,
      })));
      cursor = page.next_cursor || undefined;
    } while (cursor);
    setNodes(value => replaceChildren(value, key, children));
  }
  return <Tree showIcon treeData={nodes} selectedKeys={[keyFor(selected, path)]}
    loadData={node => load(String(node.key)).catch(error => { void message.error(String(error)); throw error; })}
    onSelect={keys => { if (keys.length) { const [space, next] = JSON.parse(String(keys[0])); onSelect(space, next); } }} />;
}
