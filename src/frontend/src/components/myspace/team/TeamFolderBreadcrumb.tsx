import { useMemo } from 'react';
import { Breadcrumb } from 'antd';
import { HomeOutlined, TeamOutlined, FolderOutlined } from '@ant-design/icons';
import { useMySpaceStore } from '../../../stores/mySpaceStore';
import type { TeamFolderNode } from '../../../types/teamFiles';
import { t } from '../../../i18n';

function findBreadcrumb(
  nodes: TeamFolderNode[],
  folderId: string,
  path: TeamFolderNode[] = [],
): TeamFolderNode[] | null {
  for (const n of nodes) {
    const cur = [...path, n];
    if (n.folder_id === folderId) return cur;
    if (n.children?.length) {
      const deeper = findBreadcrumb(n.children, folderId, cur);
      if (deeper) return deeper;
    }
  }
  return null;
}

export function TeamFolderBreadcrumb() {
  const { selectedScope, myTeams, folderTreesByTeam, setScope } = useMySpaceStore();

  const items = useMemo(() => {
    if (selectedScope.kind === 'personal') {
      return [{ title: <><HomeOutlined /> <span style={{ marginLeft: 6 }}>{t('个人文件')}</span></> }];
    }
    const team = myTeams.find((t) => t.team_id === selectedScope.teamId);
    const teamName = team?.name || t('团队');
    const teamTitle = (
      <a
        onClick={(e) => {
          e.preventDefault();
          setScope({ kind: 'team', teamId: selectedScope.teamId, folderId: null });
        }}
        style={{ cursor: 'pointer' }}
      >
        <TeamOutlined /> <span style={{ marginLeft: 6 }}>{teamName}</span>
      </a>
    );
    const list: any[] = [{ title: teamTitle }];

    if (selectedScope.folderId) {
      const tree = folderTreesByTeam[selectedScope.teamId] || [];
      const path = findBreadcrumb(tree, selectedScope.folderId) || [];
      path.forEach((node, idx) => {
        const isLast = idx === path.length - 1;
        list.push({
          title: isLast ? (
            <span><FolderOutlined /> <span style={{ marginLeft: 6 }}>{node.name}</span></span>
          ) : (
            <a
              onClick={(e) => {
                e.preventDefault();
                setScope({ kind: 'team', teamId: selectedScope.teamId, folderId: node.folder_id });
              }}
              style={{ cursor: 'pointer' }}
            >
              <FolderOutlined /> <span style={{ marginLeft: 6 }}>{node.name}</span>
            </a>
          ),
        });
      });
    }
    return list;
  }, [selectedScope, myTeams, folderTreesByTeam, setScope]);

  return (
    <div className="jx-team-breadcrumb">
      <Breadcrumb items={items} />
    </div>
  );
}
