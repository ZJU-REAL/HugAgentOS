import { useEffect, useMemo, useState } from 'react';
import { FolderOutlined } from '@ant-design/icons';
import { Input, message, Modal, Radio, TreeSelect } from 'antd';

import { listPersonalFolderTree } from '../../api';
import { t } from '../../i18n';
import { useProjectStore } from '../../stores/projectStore';
import type { PersonalFolderNode } from '../../types';

interface Props {
  onCreated?: (projectId: string) => void;
}

interface FolderTreeNode {
  value: string;
  title: string;
  children?: FolderTreeNode[];
}

function foldersToTree(folders: PersonalFolderNode[]): FolderTreeNode[] {
  return folders.map((folder) => ({
    value: folder.folder_id,
    title: folder.name,
    children: folder.children?.length ? foldersToTree(folder.children) : undefined,
  }));
}

export default function CreateProjectModal({ onCreated }: Props) {
  const open = useProjectStore((state) => state.createModalOpen);
  const setOpen = useProjectStore((state) => state.setCreateModalOpen);
  const createPersonal = useProjectStore((state) => state.createPersonal);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [folderMode, setFolderMode] = useState<'auto' | 'existing'>('auto');
  const [linkedFolderId, setLinkedFolderId] = useState<string>();
  const [folders, setFolders] = useState<PersonalFolderNode[]>([]);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setName('');
    setDescription('');
    setFolderMode('auto');
    setLinkedFolderId(undefined);
    void listPersonalFolderTree().then(setFolders).catch(() => setFolders([]));
  }, [open]);

  const folderTreeData = useMemo(() => foldersToTree(folders), [folders]);

  const handleSubmit = async () => {
    const cleanName = name.trim();
    if (!cleanName) {
      message.warning(t('请填写项目名'));
      return;
    }
    if (folderMode === 'existing' && !linkedFolderId) {
      message.warning(t('请选择要挂钩的文件夹'));
      return;
    }
    setSubmitting(true);
    try {
      const projectId = await createPersonal(
        cleanName,
        description.trim() || undefined,
        folderMode === 'existing' ? linkedFolderId : undefined,
      );
      message.success(t('项目已创建'));
      setOpen(false);
      onCreated?.(projectId);
    } catch (error) {
      message.error((error as Error)?.message || t('创建失败'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title={t('创建项目')}
      open={open}
      onCancel={() => setOpen(false)}
      onOk={handleSubmit}
      okText={t('创建项目')}
      cancelText={t('取消')}
      confirmLoading={submitting}
      destroyOnClose
      width={520}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16, paddingTop: 8 }}>
        <div>
          <div style={{ marginBottom: 6, fontWeight: 600, fontSize: 13 }}>
            {t('项目名称')} <span style={{ color: 'red' }}>*</span>
          </div>
          <Input
            placeholder={t('给项目起个名字')}
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={120}
            autoFocus
          />
        </div>
        <div>
          <div style={{ marginBottom: 6, fontWeight: 600, fontSize: 13 }}>{t('项目目标')}</div>
          <Input.TextArea
            placeholder={t('简述这个项目的目标、主题或上下文…')}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            rows={4}
            maxLength={2000}
          />
        </div>
        <div>
          <div style={{ marginBottom: 6, fontWeight: 600, fontSize: 13 }}>
            <FolderOutlined style={{ marginRight: 6 }} />
            {t('挂钩文件夹')}
          </div>
          <div style={{ fontSize: 12, color: 'var(--color-text-tertiary, #808080)', marginBottom: 8 }}>
            {t('默认在「我的空间」根目录新建一个同名文件夹；也可挂钩到已有个人文件夹。')}
          </div>
          <Radio.Group value={folderMode} onChange={(event) => setFolderMode(event.target.value)}>
            <Radio value="auto">{t('自动新建同名文件夹')}</Radio>
            <Radio value="existing">{t('挂钩到已有文件夹')}</Radio>
          </Radio.Group>
          {folderMode === 'existing' && (
            <div style={{ marginTop: 8 }}>
              <TreeSelect
                style={{ width: '100%' }}
                placeholder={folderTreeData.length === 0 ? t('暂无可选文件夹') : t('选择文件夹')}
                value={linkedFolderId}
                onChange={setLinkedFolderId}
                treeData={folderTreeData}
                treeDefaultExpandAll
                allowClear
                treeLine
                showSearch
                treeNodeFilterProp="title"
              />
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}
