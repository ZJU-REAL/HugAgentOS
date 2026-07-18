import { useEffect, useMemo, useState } from 'react';
import { Modal, Tree, Empty, message } from 'antd';
import { t } from '../../../i18n';
import type { DataNode } from 'antd/es/tree';
import { FolderOutlined, HomeOutlined } from '@ant-design/icons';
import { useMySpaceStore } from '../../../stores/mySpaceStore';
import type { PersonalFolderNode } from '../../../types';

interface Props {
  open: boolean;
  onClose: () => void;
  /** Artifact IDs to move/copy (only 1 is passed for a single selection) */
  artifactIds: string[];
  /** move=move (default, changes ownership); copy=copy (keeps the original, creates a new copy) */
  mode?: 'move' | 'copy';
  onDone?: (count: number) => void;
}

const ROOT_KEY = '__root__';
const FOLDER_KEY = (fid: string) => `f::${fid}`;

function buildTree(nodes: PersonalFolderNode[]): DataNode[] {
  return nodes.map((n) => ({
    key: FOLDER_KEY(n.folder_id),
    title: n.name,
    icon: <FolderOutlined />,
    children: n.children?.length ? buildTree(n.children) : undefined,
  }));
}

export function MoveToPersonalFolderModal({ open, onClose, artifactIds, mode = 'move', onDone }: Props) {
  const {
    personalFolderTree,
    loadPersonalFolderTree,
    moveArtifactsToPersonalFolderAction,
    copyArtifactsToPersonalFolderAction,
  } = useMySpaceStore();
  const isCopy = mode === 'copy';

  const [selectedKey, setSelectedKey] = useState<string | null>(ROOT_KEY);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (open) {
      setSelectedKey(ROOT_KEY);
      void loadPersonalFolderTree();
    }
  }, [open, loadPersonalFolderTree]);

  const treeData: DataNode[] = useMemo(() => {
    return [
      {
        key: ROOT_KEY,
        title: t('我的空间（根目录）'),
        icon: <HomeOutlined />,
        children: buildTree(personalFolderTree),
      },
    ];
  }, [personalFolderTree]);

  const handleOk = async () => {
    if (!selectedKey || artifactIds.length === 0) {
      message.warning(t('请选择目标文件夹'));
      return;
    }
    const folderId = selectedKey === ROOT_KEY ? null : selectedKey.replace(/^f::/, '');
    setLoading(true);
    try {
      const n = isCopy
        ? await copyArtifactsToPersonalFolderAction(artifactIds, folderId)
        : await moveArtifactsToPersonalFolderAction(artifactIds, folderId);
      message.success(isCopy ? t('已复制 {n} 项', { n }) : t('已移动 {n} 项', { n }));
      onDone?.(n);
      onClose();
    } catch (e: any) {
      message.error(e?.message || (isCopy ? t('复制失败') : t('移动失败')));
    } finally {
      setLoading(false);
    }
  };

  const title = isCopy
    ? (artifactIds.length > 1 ? t('复制 {n} 个文件到…', { n: artifactIds.length }) : t('复制到…'))
    : (artifactIds.length > 1 ? t('移动 {n} 个文件到…', { n: artifactIds.length }) : t('移动到…'));

  return (
    <Modal
      title={title}
      open={open}
      onCancel={onClose}
      onOk={handleOk}
      confirmLoading={loading}
      okText={isCopy ? t('复制到这里') : t('移动到这里')}
      cancelText={t('取消')}
      destroyOnClose
    >
      {treeData.length === 0 ? (
        <Empty description={t('无可选位置')} />
      ) : (
        <Tree
          showIcon
          defaultExpandAll
          selectedKeys={selectedKey ? [selectedKey] : []}
          onSelect={(keys) => {
            if (keys[0]) setSelectedKey(String(keys[0]));
          }}
          treeData={treeData}
        />
      )}
    </Modal>
  );
}
