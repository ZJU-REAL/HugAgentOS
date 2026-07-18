import { useEffect, useMemo, useState } from 'react';
import { Dropdown, Tree, message, Modal } from 'antd';
import { t } from '../../../i18n';
import type { DataNode } from 'antd/es/tree';
import {
  FolderOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  SafetyOutlined,
  MoreOutlined,
} from '@ant-design/icons';
import { useMySpaceStore } from '../../../stores/mySpaceStore';
import type {
  MyTeamItem,
  TeamFolderNode,
  TeamResolvedPermission,
} from '../../../types/teamFiles';
import { resolvedAtLeast, resolvedPermissionLabel } from '../../../utils/roles';
import {
  createTeamFolder,
  renameTeamFolder,
  deleteTeamFolder,
  getFolderAffectedCount,
} from '../../../api';

interface Props {
  onManagePermissions: (teamId: string) => void;
}

const TEAM_ROOT_KEY = (teamId: string) => `team::${teamId}::__root__`;
const FOLDER_KEY = (teamId: string, folderId: string) => `team::${teamId}::${folderId}`;

function canEditFolders(resolved: TeamResolvedPermission): boolean {
  // editor and admin can create/delete/rename folders; viewer is read-only
  return resolvedAtLeast(resolved, 'edit');
}

function canManageMembers(resolved: TeamResolvedPermission): boolean {
  return resolvedAtLeast(resolved, 'admin');
}

function titleNodeTeam(team: MyTeamItem): string {
  return `${team.name} · ${resolvedPermissionLabel(team.resolved)}`;
}

export function TeamScopeTree({ onManagePermissions }: Props) {
  const {
    selectedScope,
    setScope,
    myTeams,
    folderTreesByTeam,
    loadMyTeams,
    loadTeamFolderTree,
  } = useMySpaceStore();

  const [expanded, setExpanded] = useState<string[]>([]);

  useEffect(() => { void loadMyTeams(); }, [loadMyTeams]);

  useEffect(() => {
    // by default, expand the team that the current scope belongs to
    if (selectedScope.kind === 'team') {
      setExpanded((prev) => (
        prev.includes(TEAM_ROOT_KEY(selectedScope.teamId))
          ? prev
          : [...prev, TEAM_ROOT_KEY(selectedScope.teamId)]
      ));
    }
  }, [selectedScope]);

  const selectedKey = useMemo(() => {
    if (selectedScope.kind !== 'team') return '';
    if (!selectedScope.teamId) return '';
    if (!selectedScope.folderId) return TEAM_ROOT_KEY(selectedScope.teamId);
    return FOLDER_KEY(selectedScope.teamId, selectedScope.folderId);
  }, [selectedScope]);

  const handleCreateFolder = async (teamId: string, parentFolderId: string | null) => {
    let name = '';
    await new Promise<void>((resolve) => {
      const inputId = `jx-new-folder-${Date.now()}`;
      Modal.confirm({
        title: parentFolderId ? t('在此文件夹下新建子夹') : t('新建团队文件夹'),
        icon: <FolderOutlined />,
        content: (
          <input
            id={inputId}
            className="jx-team-folder-input"
            autoFocus
            placeholder={t('输入文件夹名称')}
            maxLength={60}
            onChange={(e) => { name = e.target.value; }}
          />
        ),
        okText: t('创建'),
        cancelText: t('取消'),
        onOk: async () => {
          const trimmed = name.trim();
          if (!trimmed) { message.warning(t('请输入文件夹名称')); throw new Error('empty'); }
          try {
            await createTeamFolder(teamId, trimmed, parentFolderId);
            await loadTeamFolderTree(teamId);
            message.success(t('已创建'));
          } catch (e: any) {
            message.error(e?.message || t('创建失败'));
            throw e;
          }
        },
        onCancel: () => resolve(),
      });
    });
  };

  const handleRename = async (teamId: string, folderId: string, currentName: string) => {
    let name = currentName;
    Modal.confirm({
      title: t('重命名文件夹'),
      icon: <EditOutlined />,
      content: (
        <input
          className="jx-team-folder-input"
          autoFocus
          defaultValue={currentName}
          maxLength={60}
          onChange={(e) => { name = e.target.value; }}
        />
      ),
      okText: t('保存'),
      cancelText: t('取消'),
      onOk: async () => {
        const trimmed = name.trim();
        if (!trimmed || trimmed === currentName) return;
        try {
          await renameTeamFolder(teamId, folderId, trimmed);
          await loadTeamFolderTree(teamId);
          message.success(t('已重命名'));
        } catch (e: any) {
          message.error(e?.message || t('重命名失败'));
          throw e;
        }
      },
    });
  };

  const handleDelete = async (teamId: string, folderId: string, folderName: string) => {
    let affected = 0;
    try { affected = await getFolderAffectedCount(teamId, folderId); } catch { /* ignore */ }
    Modal.confirm({
      title: t('删除文件夹「{name}」', { name: folderName }),
      icon: <DeleteOutlined style={{ color: '#ff4d4f' }} />,
      content: affected > 0
        ? t('该文件夹及其子目录内共有 {n} 个文件将一并被删除。此操作会级联软删，确认继续吗？', { n: affected })
        : t('该文件夹为空，确认删除吗？'),
      okText: t('删除'),
      okButtonProps: { danger: true },
      cancelText: t('取消'),
      onOk: async () => {
        try {
          await deleteTeamFolder(teamId, folderId);
          await loadTeamFolderTree(teamId);
          message.success(t('已删除'));
          // if the deleted scope is currently selected, fall back to that team's root
          if (selectedScope.kind === 'team' && selectedScope.teamId === teamId
            && selectedScope.folderId === folderId) {
            setScope({ kind: 'team', teamId, folderId: null });
          }
        } catch (e: any) {
          message.error(e?.message || t('删除失败'));
          throw e;
        }
      },
    });
  };

  const renderFolderNodes = (
    team: MyTeamItem,
    folders: TeamFolderNode[],
  ): DataNode[] => folders.map((folder) => {
    const resolved = team.resolved;
    const editable = canEditFolders(resolved);
    const menuItems = editable ? [
      {
        key: 'new-child',
        icon: <PlusOutlined />,
        label: t('新建子文件夹'),
        onClick: () => void handleCreateFolder(team.team_id, folder.folder_id),
      },
      {
        key: 'rename',
        icon: <EditOutlined />,
        label: t('重命名'),
        onClick: () => void handleRename(team.team_id, folder.folder_id, folder.name),
      },
      { type: 'divider' as const },
      {
        key: 'delete',
        icon: <DeleteOutlined />,
        danger: true,
        label: t('删除文件夹'),
        onClick: () => void handleDelete(team.team_id, folder.folder_id, folder.name),
      },
    ] : [];

    const title = (
      <Dropdown
        menu={{ items: menuItems }}
        trigger={editable ? ['contextMenu'] : []}
        disabled={!editable}
      >
        <div className="jx-team-tree-row">
          <span className="jx-team-tree-title">{folder.name}</span>
          {editable && (
            <Dropdown
              menu={{ items: menuItems }}
              trigger={['click']}
              placement="bottomRight"
            >
              <button
                type="button"
                className="jx-team-tree-more"
                aria-label={t('更多操作')}
                onClick={(e) => { e.stopPropagation(); }}
              >
                <MoreOutlined />
              </button>
            </Dropdown>
          )}
        </div>
      </Dropdown>
    );

    return {
      key: FOLDER_KEY(team.team_id, folder.folder_id),
      title,
      icon: <FolderOutlined />,
      children: folder.children?.length ? renderFolderNodes(team, folder.children) : undefined,
    };
  });

  const treeData: DataNode[] = useMemo(() => {
    const teamNodes: DataNode[] = myTeams.map((team) => {
      const tree = folderTreesByTeam[team.team_id] || [];
      const canEdit = canEditFolders(team.resolved);
      const canAdmin = canManageMembers(team.resolved);
      const rootMenuItems: any[] = [];
      if (canEdit) {
        rootMenuItems.push({
          key: 'new-root',
          icon: <PlusOutlined />,
          label: t('新建文件夹'),
          onClick: () => void handleCreateFolder(team.team_id, null),
        });
      }
      if (canAdmin) {
        if (rootMenuItems.length > 0) rootMenuItems.push({ type: 'divider' });
        rootMenuItems.push({
          key: 'manage',
          icon: <SafetyOutlined />,
          label: t('管理成员权限'),
          onClick: () => onManagePermissions(team.team_id),
        });
      }
      const hasRootMenu = rootMenuItems.length > 0;
      const rootTitle = (
        <Dropdown
          menu={{ items: rootMenuItems }}
          trigger={hasRootMenu ? ['contextMenu'] : []}
          disabled={!hasRootMenu}
        >
          <div className="jx-team-tree-row">
            <span className="jx-team-tree-title">{titleNodeTeam(team)}</span>
            {hasRootMenu && (
              <Dropdown
                menu={{ items: rootMenuItems }}
                trigger={['click']}
                placement="bottomRight"
              >
                <button
                  type="button"
                  className="jx-team-tree-more"
                  aria-label={t('更多操作')}
                  onClick={(e) => { e.stopPropagation(); }}
                >
                  <MoreOutlined />
                </button>
              </Dropdown>
            )}
          </div>
        </Dropdown>
      );

      return {
        key: TEAM_ROOT_KEY(team.team_id),
        title: rootTitle,
        className: 'jx-team-tree-teamRoot',
        children: tree.length > 0 ? renderFolderNodes(team, tree) : undefined,
      };
    });

    return teamNodes;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [myTeams, folderTreesByTeam, selectedScope]);

  return (
    <div className="jx-team-scopeTree">
      <div className="jx-team-scopeTree-header">
        <span className="jx-team-scopeTree-title">{t('文件范围')}</span>
        <span className="jx-team-scopeTree-hint">{t('共 {n} 个团队', { n: myTeams.length })}</span>
      </div>
      <Tree
        className="jx-team-scopeTree-body"
        treeData={treeData}
        showIcon
        blockNode
        defaultExpandAll={false}
        expandedKeys={expanded}
        onExpand={(keys, info) => {
          setExpanded(keys as string[]);
          // if a team root is expanded, lazy-load its folder tree
          const k = (info.node.key as string);
          if (k.startsWith('team::')) {
            const teamId = k.split('::')[1];
            if (info.expanded && !folderTreesByTeam[teamId]) {
              void loadTeamFolderTree(teamId);
            }
          }
        }}
        selectedKeys={selectedKey ? [selectedKey] : []}
        onSelect={(_keys, info) => {
          const key = info.node.key as string;
          const parts = key.split('::');
          if (parts[0] !== 'team' || parts.length < 3) return;
          const teamId = parts[1];
          const folderIdRaw = parts[2];
          const folderId = folderIdRaw === '__root__' ? null : folderIdRaw;
          // clicking any node (team root or folder) toggles expand/collapse
          setExpanded((prev) => (
            prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]
          ));
          setScope({ kind: 'team', teamId, folderId });
          if (!folderTreesByTeam[teamId]) void loadTeamFolderTree(teamId);
        }}
      />
    </div>
  );
}
