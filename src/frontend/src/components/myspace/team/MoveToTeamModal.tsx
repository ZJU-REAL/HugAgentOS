import { useEffect, useMemo, useState } from 'react';
import { Modal, Tree, Empty, Tag, message } from 'antd';
import { t } from '../../../i18n';
import type { DataNode } from 'antd/es/tree';
import { FolderOutlined, TeamOutlined } from '@ant-design/icons';
import { useMySpaceStore } from '../../../stores/mySpaceStore';
import type { TeamFolderNode } from '../../../types/teamFiles';
import { resolvedAtLeast, resolvedPermissionLabel } from '../../../utils/roles';

interface Props {
  open: boolean;
  onClose: () => void;
  /** Source artifact IDs (used for personal-side batch operations). */
  personalArtifactIds?: string[];
  /** Move a single file within a team. */
  teamArtifactId?: string;
  /** Required for intra-team moves; used to lock the selectable target to this team. */
  lockedTeamId?: string;
  /** 'move' (default, destructive move) or 'copy' (non-destructive copy, keeps the personal original). */
  mode?: 'move' | 'copy';
  /** If given in copy mode, recursively copies this personal folder (alternative to personalArtifactIds). */
  personalFolderId?: string;
  onDone?: (movedCount: number) => void;
}

const ROOT_KEY = (teamId: string) => `team::${teamId}::__root__`;
const FOLDER_KEY = (teamId: string, fid: string) => `team::${teamId}::${fid}`;

function nodesToTree(teamId: string, folders: TeamFolderNode[]): DataNode[] {
  return folders.map((f) => ({
    key: FOLDER_KEY(teamId, f.folder_id),
    title: f.name,
    icon: <FolderOutlined />,
    children: f.children?.length ? nodesToTree(teamId, f.children) : undefined,
  }));
}

export function MoveToTeamModal({
  open, onClose, personalArtifactIds, teamArtifactId, lockedTeamId, mode = 'move', personalFolderId, onDone,
}: Props) {
  const {
    myTeams,
    folderTreesByTeam,
    loadMyTeams,
    loadTeamFolderTree,
    movePersonalToTeam,
    copyPersonalToTeam,
    copyFolderToTeam,
    moveWithinTeam,
  } = useMySpaceStore();
  const isCopy = mode === 'copy';

  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const eligibleTeams = useMemo(() => {
    const writable = myTeams.filter((t) => resolvedAtLeast(t.resolved, 'edit'));
    return lockedTeamId ? writable.filter((t) => t.team_id === lockedTeamId) : writable;
  }, [myTeams, lockedTeamId]);

  useEffect(() => {
    if (!open) return;
    void loadMyTeams();
    eligibleTeams.forEach((t) => {
      if (!folderTreesByTeam[t.team_id]) void loadTeamFolderTree(t.team_id);
    });
  }, [open, eligibleTeams, folderTreesByTeam, loadMyTeams, loadTeamFolderTree]);

  const treeData: DataNode[] = useMemo(() => eligibleTeams.map((team) => ({
    key: ROOT_KEY(team.team_id),
    title: <span>{team.name} <Tag color={team.resolved === 'admin' ? 'gold' : 'blue'} style={{ marginLeft: 6 }}>{resolvedPermissionLabel(team.resolved)}</Tag></span>,
    icon: <TeamOutlined />,
    children: nodesToTree(team.team_id, folderTreesByTeam[team.team_id] || []),
  })), [eligibleTeams, folderTreesByTeam]);

  const handleOk = async () => {
    if (!selectedKey) {
      message.warning(t('请选择目标团队或文件夹'));
      return;
    }
    const parts = selectedKey.split('::');
    if (parts[0] !== 'team') return;
    const teamId = parts[1];
    const folderIdRaw = parts[2];
    const folderId = folderIdRaw === '__root__' ? null : folderIdRaw;

    setLoading(true);
    try {
      if (isCopy && personalFolderId) {
        const r = await copyFolderToTeam(personalFolderId, teamId, folderId);
        message.success(t('已复制到团队（{f} 个文件夹、{n} 个文件）', { f: r.folders, n: r.files }));
        onDone?.(r.files);
      } else if (isCopy && personalArtifactIds && personalArtifactIds.length > 0) {
        const copied = await copyPersonalToTeam(personalArtifactIds, teamId, folderId);
        if (copied === personalArtifactIds.length) {
          message.success(t('已复制 {n} 个文件到团队', { n: copied }));
        } else {
          message.warning(t('成功 {moved} / {total}，部分失败', { moved: copied, total: personalArtifactIds.length }));
        }
        onDone?.(copied);
      } else if (teamArtifactId && lockedTeamId) {
        await moveWithinTeam(teamArtifactId, folderId);
        message.success(t('已移动'));
        onDone?.(1);
      } else if (personalArtifactIds && personalArtifactIds.length > 0) {
        const moved = await movePersonalToTeam(personalArtifactIds, teamId, folderId);
        if (moved === personalArtifactIds.length) {
          message.success(t('已移动 {n} 个文件到团队', { n: moved }));
        } else {
          message.warning(t('成功 {moved} / {total}，部分失败', { moved, total: personalArtifactIds.length }));
        }
        onDone?.(moved);
      }
      onClose();
      setSelectedKey(null);
    } catch (e: any) {
      message.error(e?.message || (isCopy ? t('复制失败') : t('移动失败')));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      open={open}
      onCancel={() => { if (!loading) { onClose(); setSelectedKey(null); } }}
      onOk={handleOk}
      confirmLoading={loading}
      title={isCopy ? t('复制到团队文件夹') : (teamArtifactId ? t('移动到其他文件夹') : t('移动到团队文件夹'))}
      okText={isCopy ? t('复制') : t('移动')}
      cancelText={t('取消')}
      width={520}
    >
      {eligibleTeams.length === 0 ? (
        <Empty description={t('当前没有可写入的团队')} />
      ) : (
        <Tree
          className="jx-team-moveTree"
          treeData={treeData}
          showIcon
          blockNode
          selectedKeys={selectedKey ? [selectedKey] : []}
          onSelect={(keys) => setSelectedKey(keys[0] as string)}
          defaultExpandAll
        />
      )}
    </Modal>
  );
}
