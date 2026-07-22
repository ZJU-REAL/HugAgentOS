import type { AuthUser } from './api';

export const EDITION_SETTINGS_SECTIONS = [];

export function EditionProfileMemberships({ authUser: _authUser }: { authUser: AuthUser | null }) {
  return null;
}

export function EditionSettingsContent({ activeSection: _activeSection, enabled: _enabled }: { activeSection: string; enabled: boolean }) {
  return null;
}
