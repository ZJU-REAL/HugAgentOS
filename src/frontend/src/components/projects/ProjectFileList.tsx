import { memo, useMemo, useState } from 'react';
import { Button, Tree } from 'antd';
import { DeleteOutlined, EyeOutlined, FolderOutlined } from '@ant-design/icons';
import type { DataNode } from 'antd/es/tree';
import type { ProjectFileItem } from '../../types';
import { t } from '../../i18n';
import { fmtBytes, leafName, shortType } from './projectFileDisplay';

interface FileNode extends DataNode {
  file?: ProjectFileItem;
  children?: FileNode[];
}
interface Props {
  files: ProjectFileItem[];
  canEdit: boolean;
  onPreview: (file: ProjectFileItem) => void;
  onDelete: (file: ProjectFileItem) => void;
}

/** Mount controls only for viewport rows, even when one folder has thousands of files. */
export default memo(function ProjectFileList({ files, canEdit, onPreview, onDelete }: Props) {
  const treeData = useMemo(() => {
    const groups = new Map<string, FileNode>();
    const loose: FileNode[] = [];
    for (const file of files) {
      const node: FileNode = { key: 'file:' + file.id, title: leafName(file), file, isLeaf: true };
      const top = (file.folder_path || '').split('/', 1)[0];
      if (!top) {
        loose.push(node);
        continue;
      }
      let group = groups.get(top);
      if (!group) {
        group = { key: 'folder:' + top, title: top, children: [] };
        groups.set(top, group);
      }
      group.children!.push(node);
    }
    return [...groups.values(), ...loose];
  }, [files]);

  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const expandedKeys = treeData.filter(node => node.children && !collapsed.has(String(node.key))).map(node => node.key);

  return (
    <Tree<FileNode>
      className="jx-projectRail-fileTree"
      treeData={treeData}
      height={320}
      itemHeight={54}
      virtual
      blockNode
      selectable={false}
      expandedKeys={expandedKeys}
      onExpand={(keys) => {
        const expanded = new Set(keys);
        setCollapsed(new Set(treeData.filter(node => node.children && !expanded.has(node.key)).map(node => String(node.key))));
      }}
      onClick={(_, node) => {
        if (node.file) return;
        const key = String(node.key);
        setCollapsed(previous => {
          const next = new Set(previous);
          if (next.has(key)) next.delete(key);
          else next.add(key);
          return next;
        });
      }}
      titleRender={(node) => {
        const file = node.file;
        if (!file) return (
          <span className="jx-projectRail-groupHeader">
            <FolderOutlined />
            <span className="jx-projectRail-groupName" title={String(node.title)}>{node.title as string}</span>
            <span className="jx-projectRail-groupCount">{node.children?.length}</span>
          </span>
        );
        return (
          <div className="jx-projectRail-fileItem">
            <button type="button" className="jx-projectRail-fileInfo jx-projectRail-filePreview"
              onClick={() => onPreview(file)} title={file.name}>
              <span className="jx-projectRail-fileName">{leafName(file)}</span>
              <span className="jx-projectRail-fileMeta">{shortType(file)} · {fmtBytes(file.size_bytes || 0)}</span>
            </button>
            <Button type="text" size="small" icon={<EyeOutlined />} title={t('预览')}
              onClick={() => onPreview(file)} />
            {canEdit && (
              <Button type="text" size="small" icon={<DeleteOutlined />} title={t('删除')}
                onClick={() => onDelete(file)} />
            )}
          </div>
        );
      }}
    />
  );
});
