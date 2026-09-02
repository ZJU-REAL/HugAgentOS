import { useEffect, useSyncExternalStore, type ReactNode } from 'react';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import { getLang } from './i18n';

// antd 的 locale 只翻译按钮/占位文案，日期面板里的「年/月/周几」走的是 dayjs 自己的 locale。
// 不设这一行，DatePicker / TimePicker 面板就是 Jan、Mo、Tu 这种英文，与中文界面割裂。
dayjs.locale(getLang() === 'en' ? 'en' : 'zh-cn');
import { getAppTheme } from './appTheme';
import { useUIStore } from './stores/uiStore';
import {
  applyThemeToDom,
  systemPrefersDark,
  systemPrefersReducedMotion,
  watchReducedMotion,
  watchSystemTheme,
} from './theme';

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
  // 系统开了「减弱动画」就顺手关掉 antd 自己的过渡（它的加载转圈不受影响，见 appTheme.ts）
  const reducedMotion = useSyncExternalStore(watchReducedMotion, systemPrefersReducedMotion);

  useEffect(() => {
    applyThemeToDom(isDark);
  }, [isDark]);

  return (
    <ConfigProvider theme={getAppTheme(isDark, reducedMotion)} locale={getLang() === 'en' ? enUS : zhCN}>
      {children}
    </ConfigProvider>
  );
}
