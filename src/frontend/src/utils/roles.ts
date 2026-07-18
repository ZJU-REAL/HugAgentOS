/**
 * Role / team permission constants —— single source of truth, mirroring backend
 * `core/auth/roles.py` and `core/auth/team_permissions.py`. All components should
 * import TeamRole / TeamFilePermission / TeamResolvedPermission and the
 * corresponding label / rank helpers from here, to avoid duplicating the unions
 * and Chinese labels in many places.
 */

import { t } from '../i18n';

export type TeamRole = 'owner' | 'admin' | 'member';
export type TeamFilePermission = 'viewer' | 'editor';
export type TeamResolvedPermission = 'none' | 'view' | 'edit' | 'admin';

export const TEAM_ROLES: readonly TeamRole[] = ['owner', 'admin', 'member'] as const;

export const ROLE_RANK: Record<TeamRole, number> = {
  member: 1,
  admin: 2,
  owner: 3,
};

export const ROLE_LABELS: Record<TeamRole, string> = {
  owner: t('所有者'),
  admin: t('管理员'),
  member: t('成员'),
};

export const FILE_PERMISSION_LABELS: Record<TeamFilePermission, string> = {
  viewer: t('仅可读'),
  editor: t('可编辑'),
};

export const RESOLVED_PERMISSION_LABELS: Record<TeamResolvedPermission, string> = {
  none: t('无权限'),
  view: t('只读'),
  edit: t('可编辑'),
  admin: t('管理员'),
};

const RESOLVED_RANK: Record<TeamResolvedPermission, number> = {
  none: 0,
  view: 1,
  edit: 2,
  admin: 3,
};

export function roleLabel(role: string): string {
  return (ROLE_LABELS as Record<string, string>)[role] || role;
}

export function roleRank(role: string): number {
  return (ROLE_RANK as Record<string, number>)[role] ?? 0;
}

export function roleAtLeast(role: string, minimum: TeamRole): boolean {
  return roleRank(role) >= roleRank(minimum);
}

export function filePermissionLabel(perm: string): string {
  return (FILE_PERMISSION_LABELS as Record<string, string>)[perm] || perm;
}

export function resolvedPermissionLabel(perm: string): string {
  return (RESOLVED_PERMISSION_LABELS as Record<string, string>)[perm] || perm;
}

/**
 * Determine whether the currently resolved team file permission meets the required level.
 * Replaces the scattered `perm === 'edit' || perm === 'admin'` checks.
 */
export function resolvedAtLeast(
  current: TeamResolvedPermission | string | null | undefined,
  required: TeamResolvedPermission,
): boolean {
  const c = (RESOLVED_RANK as Record<string, number>)[current ?? 'none'] ?? 0;
  return c >= RESOLVED_RANK[required];
}

