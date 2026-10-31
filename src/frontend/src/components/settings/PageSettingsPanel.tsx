import { Select } from 'antd';
import { getLang, setLang, t, type Lang } from '../../i18n';
import { useUIStore } from '../../stores';
import type { ThemeMode } from '../../theme';
import { SidebarLayoutSetting } from './SidebarLayoutSetting';

export function PageSettingsPanel() {
  const { themeMode, setThemeMode } = useUIStore();
  return (
        <section id="section-session" className="jx-settings-section">
        <h3 className="jx-settings-section-title">{t('页面设置')}</h3>
        <div className="jx-settings-card">
          <div className="jx-settings-row">
            <div className="jx-settings-rowLeft">
              <span className="jx-settings-rowLabel">{t('界面语言')}</span>
              <span className="jx-settings-rowDesc">
                {t('切换界面显示语言，切换后页面将自动刷新')}
              </span>
            </div>
            <Select
              value={getLang()}
              style={{ width: 140 }}
              options={[
                { value: 'zh-CN', label: t('简体中文') },
                { value: 'en', label: 'English' },
              ]}
              onChange={(v) => setLang(v as Lang)}
            />
          </div>

          <div className="jx-settings-divider" />

          <div className="jx-settings-row">
            <div className="jx-settings-rowLeft">
              <span className="jx-settings-rowLabel">{t('外观')}</span>
              <span className="jx-settings-rowDesc">
                {t('切换界面深浅配色，选择跟随系统时自动匹配系统外观')}
              </span>
            </div>
            <Select
              value={themeMode}
              style={{ width: 140 }}
              options={[
                { value: 'system', label: t('跟随系统') },
                { value: 'light', label: t('浅色') },
                { value: 'dark', label: t('深色') },
              ]}
              onChange={(v) => setThemeMode(v as ThemeMode)}
            />
          </div>
        </div>
        <SidebarLayoutSetting />
        </section>
  );
}
