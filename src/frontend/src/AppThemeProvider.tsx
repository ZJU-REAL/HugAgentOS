import { useEffect, useSyncExternalStore, type ReactNode } from 'react';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import { getLang } from './i18n';
import { getAppTheme } from './appTheme';
import { useUIStore } from './stores/uiStore';
import { applyThemeToDom, systemPrefersDark, watchSystemTheme } from './theme';

interface AppThemeProviderProps {
  children: ReactNode;
  /** 分享预览等对外页面锁定浅色：观看者常不是本机账号，内容外观应与作者所见一致 */
  forceLight?: boolean;
}

/**
 * 主题根：统一包掉 antd ConfigProvider（含 locale），按 uiStore 的档位 + 系统外观
 * 解析当前深浅，切换 light/dark algorithm 并把 data-theme 落 DOM。
 * 「跟随系统」经 useSyncExternalStore 订阅 matchMedia，系统翻转即时生效。
 * 主入口与 CE overlay 入口共用，入口文件各自只包一层。
 */
export function AppThemeProvider({ children, forceLight = false }: AppThemeProviderProps) {
  const themeMode = useUIStore((s) => s.themeMode);
  const sysDark = useSyncExternalStore(watchSystemTheme, systemPrefersDark);
  const isDark = !forceLight && (themeMode === 'dark' || (themeMode === 'system' && sysDark));

  useEffect(() => {
    applyThemeToDom(isDark);
  }, [isDark]);

  return (
    <ConfigProvider theme={getAppTheme(isDark)} locale={getLang() === 'en' ? enUS : zhCN}>
      {children}
    </ConfigProvider>
  );
}
