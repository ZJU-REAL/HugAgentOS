import { Button } from 'antd';
import { FileTextOutlined } from '@ant-design/icons';
import { t } from './i18n';
import type { AuthUser } from './api';

export const EDITION_SETTINGS_SECTIONS = [{ id: 'prompts', label: t('提示词管理'), icon: <FileTextOutlined /> }];

export function EditionProfileMemberships({ authUser: _authUser }: { authUser: AuthUser | null }) {
  return null;
}

export function EditionSettingsContent({ activeSection: _activeSection, enabled: _enabled }: { activeSection: string; enabled: boolean }) {
  return !_enabled || _activeSection !== 'prompts' ? null : <section className="jx-settings-section"><Button href="/config">{t('进入提示词管理')}</Button></section>;
}
