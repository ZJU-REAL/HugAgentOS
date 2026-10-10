import { memo, useMemo, useState } from 'react';
import { Button, Tree } from 'antd';
import { DeleteOutlined, EyeOutlined, FolderOutlined } from '@ant-design/icons';
import type { DataNode } from 'antd/es/tree';
import type { ProjectFileItem } from '../../types';
import { t } from '../../i18n';
import { fmtBytes, leafName } from './projectFileDisplay';

interface FileNode extends DataNode {
  file?: ProjectFileItem;
  folderPath?: string;
  fileCount?: number;
  children?: FileNode[];
}
interface Props {
  files: ProjectFileItem[];
  canEdit: boolean;
  onPreview: (file: ProjectFileItem) => void;
  onDelete: (file: ProjectFileItem) => void;
  onDeleteFolder: (name: string) => void;
}

/** Mount controls only for viewport rows, even when one folder has thousands of files. */
export default memo(function ProjectFileList({ files, canEdit, onPreview, onDelete, onDeleteFolder }: Props) {
  const treeData = useMemo(() => {
    const folders = new Map<string, FileNode>();
    const roots: FileNode[] = [];
    for (const file of files) {
      let siblings = roots;
      let path = '';
      for (const part of (file.folder_path || '').split('/').filter(Boolean)) {
        path = path ? `${path}/${part}` : part;
        let folder = folders.get(path);
        if (!folder) {
          folder = { key: 'folder:' + path, title: part, folderPath: path, fileCount: 0, children: [] };
          folders.set(path, folder);
          siblings.push(folder);
        }
        folder.fileCount! += 1;
        siblings = folder.children!;
      }
      siblings.push({ key: 'file:' + file.id, title: leafName(file), file, isLeaf: true });
    }
    // Keep folders before files at every level without sorting the whole inventory.
    for (const nodes of [roots, ...Array.from(folders.values(), folder => folder.children!)]) {
      nodes.sort((a, b) => Number(!!a.file) - Number(!!b.file));
    }
    return roots;
  }, [files]);

  const [expandedKeys, setExpandedKeys] = useState<React.Key[]>([]);

  return (
    <Tree<FileNode>
      className="jx-projectRail-fileTree"
      treeData={treeData}
      height={320}
      itemHeight={38}
      switcherIcon={null}
      virtual
      blockNode
      selectable={false}
      expandedKeys={expandedKeys}
      onExpand={setExpandedKeys}
      onClick={(_, node) => {
        if (node.file) return;
        setExpandedKeys(previous => previous.includes(node.key)
          ? previous.filter(key => key !== node.key)
          : [...previous, node.key]);
      }}
      titleRender={(node) => {
        const file = node.file;
        if (!file) return (
          <span className="jx-projectRail-groupHeader">
            <FolderOutlined />
            <span className="jx-projectRail-groupName" title={String(node.title)}>{node.title as string}</span>
            <span className="jx-projectRail-groupCount">{node.fileCount}</span>
            {canEdit && (
              <span className="jx-projectRail-rowActions">
                <Button type="text" size="small" icon={<DeleteOutlined />} title={t('删除文件夹')}
                  aria-label={t('删除文件夹')}
                  onClick={(event) => {
                    event.stopPropagation();
                    onDeleteFolder(node.folderPath!);
                  }} />
              </span>
            )}
          </span>
        );
        return (
          <div className="jx-projectRail-fileItem">
            <button type="button" className="jx-projectRail-fileInfo jx-projectRail-filePreview"
              onClick={() => onPreview(file)} title={file.name}>
              <span className="jx-projectRail-fileName">{leafName(file)}</span>
              <span className="jx-projectRail-fileMeta">{fmtBytes(file.size_bytes || 0)}</span>
            </button>
            <span className="jx-projectRail-rowActions">
              <Button type="text" size="small" icon={<EyeOutlined />} title={t('预览')} aria-label={t('预览')}
                onClick={() => onPreview(file)} />
              {canEdit && (
                <Button type="text" size="small" icon={<DeleteOutlined />} title={t('删除')} aria-label={t('删除')}
                  onClick={() => onDelete(file)} />
              )}
            </span>
          </div>
        );
      }}
    />
  );
});
