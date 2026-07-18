/** Team folder / team file type definitions. */

// Role / permission types are exported uniformly from utils/roles.ts to avoid duplicate definitions
import type {
  TeamRole,
  TeamFilePermission,
  TeamResolvedPermission,
} from '../utils/roles';

export type { TeamRole, TeamFilePermission, TeamResolvedPermission };

export interface MyTeamItem {
  team_id: string;
  name: string;
  description?: string | null;
  role: TeamRole;
  file_permission: TeamFilePermission;
  resolved: TeamResolvedPermission;
}

export interface TeamFolderNode {
  folder_id: string;
  team_id: string;
  parent_folder_id: string | null;
  name: string;
  created_by?: string | null;
  created_at?: string | null;
  children: TeamFolderNode[];
}

export interface TeamFolderFlat {
  folder_id: string;
  team_id: string;
  parent_folder_id: string | null;
  name: string;
  created_by?: string | null;
  created_at?: string | null;
}

export interface TeamMemberPermission {
  user_id: string;
  username: string;
  avatar_url?: string | null;
  role: TeamRole;
  file_permission: TeamFilePermission;
  joined_at?: string | null;
}

/** Frontend scope description: personal (with an optional personal folder) or a specific folder in a specific team. */
export type FileScope =
  | { kind: 'personal'; folderId?: string | null }
  | { kind: 'team'; teamId: string; folderId: string | null };

export function sameScope(a: FileScope, b: FileScope): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === 'personal') {
    return (a.folderId ?? null) === ((b as any).folderId ?? null);
  }
  return a.teamId === (b as any).teamId && a.folderId === (b as any).folderId;
}

export function scopeCacheKey(s: FileScope): string {
  if (s.kind === 'personal') return `personal:${s.folderId ?? '__root__'}`;
  return `team:${s.teamId}:${s.folderId ?? '__root__'}`;
}
