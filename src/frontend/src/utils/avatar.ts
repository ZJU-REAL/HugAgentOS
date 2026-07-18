export function resolveAvatarUrl(avatarUrl?: string | null): string {
  return avatarUrl || '/home/default-avatar.svg';
}
