import { useEffect, useRef, useState } from 'react';
import { Tree, message } from 'antd';
import type { DataNode } from 'antd/es/tree';
import { TeamOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { getFolderIconSrc } from '../../utils/fileIcon';
import { listSpaceEntries, type SpaceRoot } from './spaceFilesApi';

const keyFor = (space: string, path: string) => JSON.stringify([space, path]);
const parseKey = (key: string) => JSON.parse(key) as [string, string];
interface Folder { path: string; name: string }
interface Props {
  roots: SpaceRoot[]; selected: string; path: string; revision: number;
  onSelect: (space: string, path: string) => void;
}
export function SpaceDirectoryTree(props: Props) {
  const identity = JSON.stringify(props.roots.map(root => [root.space_id, root.name, root.permission]));
  return <DirectoryTree key={identity + props.revision} {...props} />;
}
function DirectoryTree({ roots, selected, path, onSelect }: Props) {
  // children[key] === undefined means not loaded yet; click-to-expand replaces the switcher arrow.
  const [children, setChildren] = useState<Record<string, Folder[]>>({});
  const loading = useRef(new Map<string, Promise<void>>());

  function load(key: string) {
    const pending = loading.current.get(key);
    if (pending) return pending;
    const run = (async () => {
      const [space, parent] = parseKey(key);
      const folders: Folder[] = [];
      let cursor: string | undefined;
      do {
        const page = await listSpaceEntries(space, parent, cursor);
        folders.push(...page.entries.filter(entry => entry.is_directory).map(entry => ({ path: entry.path, name: entry.name })));
        cursor = page.next_cursor || undefined;
      } while (cursor);
      setChildren(value => ({ ...value, [key]: folders }));
    })().catch(error => {
      loading.current.delete(key);
      void message.error(t('读取文件夹失败：{reason}', { reason: String(error) }));
    });
    loading.current.set(key, run);
    return run;
  }

  useEffect(() => { roots.forEach(root => void load(keyFor(root.space_id, ''))); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Entering a folder from the file list expands its ancestors so the tree always shows where you are.
  const parts = path ? path.split('/') : [];
  const ancestors = selected ? parts.map((_, index) => keyFor(selected, parts.slice(0, index).join('/'))) : [];
  const location = keyFor(selected, path);
  // The tree remounts on every reload, so start with the teams and the path to the current folder open.
  const [expanded, setExpanded] = useState<string[]>(() => [...new Set([...roots.map(root => keyFor(root.space_id, '')), ...ancestors])]);
  const [expandedFor, setExpandedFor] = useState(location);
  if (expandedFor !== location) {
    setExpandedFor(location);
    setExpanded(value => [...new Set([...value, ...ancestors])]);
  }
  useEffect(() => { ancestors.forEach(key => void load(key)); }, [location]); // eslint-disable-line react-hooks/exhaustive-deps

  const folderNodes = (space: string, parent: string): DataNode[] | undefined => {
    const list = children[keyFor(space, parent)];
    return list?.map(folder => {
      const key = keyFor(space, folder.path);
      const open = expanded.includes(key) && !!children[key]?.length;
      return {
        key, isLeaf: children[key]?.length === 0,
        icon: <img className="jx-spaceTree-folderIcon" src={getFolderIconSrc(open)} alt="" aria-hidden="true" />,
        title: <span className="jx-spaceTree-name" title={folder.name}>{folder.name}</span>,
        children: folderNodes(space, folder.path),
      };
    });
  };
  const nodes: DataNode[] = roots.map(root => ({
    key: keyFor(root.space_id, ''), className: 'jx-spaceTree-root',
    icon: <span className="jx-spaceTree-rootIcon" aria-hidden="true"><TeamOutlined /></span>,
    title: <span className="jx-spaceTree-rootTitle">
      <span className="jx-spaceTree-name" title={root.name}>{root.name}</span>
      {root.permission === 'view' && <span className="jx-spaceTree-badge">{t('仅查看')}</span>}
    </span>,
    children: folderNodes(root.space_id, ''),
  }));

  function toggle(key: string) {
    const isSelected = key === keyFor(selected, path);
    const isOpen = expanded.includes(key);
    // A second click on the selected row collapses it; selecting a new row always opens it.
    if (isSelected && isOpen) setExpanded(value => value.filter(item => item !== key));
    else if (!isOpen) {
      setExpanded(value => [...value, key]);
      void load(key);
    }
  }

  return <Tree className="jx-spaceTree" showIcon blockNode treeData={nodes}
    expandedKeys={expanded} selectedKeys={[keyFor(selected, path)]}
    onExpand={keys => setExpanded(keys.map(String))}
    onSelect={(_keys, info) => {
      const key = String(info.node.key);
      toggle(key);
      const [space, next] = parseKey(key);
      onSelect(space, next);
    }} />;
}
