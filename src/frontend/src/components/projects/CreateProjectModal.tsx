import { useEffect, useState, useMemo } from 'react';
import { Modal, Input, Radio, Select, Tooltip, TreeSelect, message } from 'antd';
import { FolderOutlined } from '@ant-design/icons';
import { useProjectStore } from '../../stores/projectStore';
import { listPersonalFolderTree, listTeamFolderTree } from '../../api';
import type { PersonalFolderNode } from '../../types';
import type { TeamFolderNode } from '../../types/teamFiles';
import { t } from '../../i18n';

interface Props {
  onCreated?: (projectId: string) => void;
}

interface ScopeTreeNode {
  value: string;
  title: string;
  children?: ScopeTreeNode[];
}

function foldersToTree<T extends { folder_id: string; name: string; children?: T[] }>(
  folders: T[],
): ScopeTreeNode[] {
  return folders.map((f) => ({
    value: f.folder_id,
    title: f.name,
    children: f.children?.length ? foldersToTree(f.children) : undefined,
  }));
}

export default function CreateProjectModal({ onCreated }: Props) {
  const open = useProjectStore((s) => s.createModalOpen);
  const setOpen = useProjectStore((s) => s.setCreateModalOpen);
  const teams = useProjectStore((s) => s.availableTeams);
  const loadTeams = useProjectStore((s) => s.loadTeamTargets);
  const createPersonal = useProjectStore((s) => s.createPersonal);
  const createTeam = useProjectStore((s) => s.createTeam);

  const [kind, setKind] = useState<'personal' | 'team'>('personal');
  const [teamId, setTeamId] = useState<string | undefined>(undefined);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [folderMode, setFolderMode] = useState<'auto' | 'existing'>('auto');
  const [linkedFolderId, setLinkedFolderId] = useState<string | undefined>(undefined);
  const [personalTree, setPersonalTree] = useState<PersonalFolderNode[]>([]);
  const [teamTree, setTeamTree] = useState<TeamFolderNode[]>([]);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) return;
    void loadTeams();
    setName('');
    setDescription('');
    setKind('personal');
    setTeamId(undefined);
    setFolderMode('auto');
    setLinkedFolderId(undefined);
    // Fetch personal folder tree
    void listPersonalFolderTree()
      .then(setPersonalTree)
      .catch(() => setPersonalTree([]));
  }, [open, loadTeams]);

  // Fetch the corresponding team folder tree when switching teams
  useEffect(() => {
    if (!open || kind !== 'team' || !teamId) {
      setTeamTree([]);
      setLinkedFolderId(undefined);
      return;
    }
    void listTeamFolderTree(teamId)
      .then(setTeamTree)
      .catch(() => setTeamTree([]));
  }, [open, kind, teamId]);

  const canTeam = teams.length > 0;

  const folderTreeData: ScopeTreeNode[] = useMemo(() => {
    if (kind === 'personal') return foldersToTree(personalTree);
    return foldersToTree(teamTree);
  }, [kind, personalTree, teamTree]);

  const handleSubmit = async () => {
    const cleanName = name.trim();
    if (!cleanName) {
      message.warning(t('请填写项目名'));
      return;
    }
    if (kind === 'team' && !teamId) {
      message.warning(t('请选择所属团队'));
      return;
    }
    if (folderMode === 'existing' && !linkedFolderId) {
      message.warning(t('请选择要挂钩的文件夹'));
      return;
    }
    setSubmitting(true);
    try {
      const linked = folderMode === 'existing' ? linkedFolderId : undefined;
      const pid = kind === 'personal'
        ? await createPersonal(cleanName, description.trim() || undefined, linked)
        : await createTeam(teamId!, cleanName, description.trim() || undefined, linked);
      message.success(t('项目已创建'));
      setOpen(false);
      onCreated?.(pid);
    } catch (err) {
      message.error((err as Error)?.message || t('创建失败'));
    } finally {
      setSubmitting(false);
    }
  };

  const folderPickerHint = kind === 'personal'
    ? t('默认在「我的空间」根目录新建一个同名文件夹；也可挂钩到已有个人文件夹。')
    : t('默认在所选团队的根目录新建一个同名团队文件夹；也可挂钩到已有团队文件夹。');

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
          <div style={{ marginBottom: 6, fontWeight: 600, fontSize: 13 }}>{t('项目类型')}</div>
          <Radio.Group value={kind} onChange={(e) => setKind(e.target.value)}>
            <Radio value="personal">{t('个人项目')}</Radio>
            <Tooltip title={canTeam ? '' : t('你不是任何团队的所有者 / 管理员，无法创建团队项目')}>
              <Radio value="team" disabled={!canTeam}>{t('团队项目')}</Radio>
            </Tooltip>
          </Radio.Group>
        </div>

        {kind === 'team' && (
          <div>
            <div style={{ marginBottom: 6, fontWeight: 600, fontSize: 13 }}>
              {t('所属团队')} <span style={{ color: 'red' }}>*</span>
            </div>
            <Select
              style={{ width: '100%' }}
              placeholder={t('选择团队')}
              value={teamId}
              onChange={(v) => { setTeamId(v); setLinkedFolderId(undefined); }}
              options={teams.map((tm) => ({ value: tm.team_id, label: `${tm.name}（${tm.role === 'owner' ? t('所有者') : t('管理员')}）` }))}
            />
          </div>
        )}

        <div>
          <div style={{ marginBottom: 6, fontWeight: 600, fontSize: 13 }}>
            {t('项目名称')} <span style={{ color: 'red' }}>*</span>
          </div>
          <Input
            placeholder={t('给项目起个名字')}
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={120}
            autoFocus
          />
        </div>

        <div>
          <div
            style={{
              marginBottom: 6,
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              fontSize: 13,
            }}
          >
            <span style={{ fontWeight: 600 }}>{t('项目目标')}</span>
            <span style={{ color: 'var(--color-text-tertiary, #808080)', fontSize: 12 }}>
              {description.length}/2000
            </span>
          </div>
          <Input.TextArea
            placeholder={t('简述这个项目的目标、主题或上下文…')}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
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
            {folderPickerHint}
          </div>
          <Radio.Group value={folderMode} onChange={(e) => setFolderMode(e.target.value)}>
            <Radio value="auto">{t('自动新建同名文件夹')}</Radio>
            <Radio value="existing">{t('挂钩到已有文件夹')}</Radio>
          </Radio.Group>
          {folderMode === 'existing' && (
            <div style={{ marginTop: 8 }}>
              <TreeSelect
                style={{ width: '100%' }}
                placeholder={
                  kind === 'team' && !teamId
                    ? t('请先选择团队')
                    : (folderTreeData.length === 0 ? t('暂无可选文件夹') : t('选择文件夹'))
                }
                value={linkedFolderId}
                onChange={setLinkedFolderId}
                treeData={folderTreeData}
                treeDefaultExpandAll
                allowClear
                disabled={kind === 'team' && !teamId}
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
