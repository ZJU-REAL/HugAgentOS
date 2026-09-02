/**
 * Ant Design theme tokens aligned with UI design spec — a single shared point for main.tsx (including the CE overlay version).
 * 深浅两套 token 的取值与 styles/variables.css 的 :root / :root[data-theme="dark"] 覆盖块一一对应，两边同步改。
 */
import { theme as antdTheme } from 'antd';
import type { ThemeConfig } from 'antd';

const sharedToken = {
  colorWarning: '#F8AB42',
  borderRadius: 10,
  borderRadiusLG: 16,
  borderRadiusSM: 6,
  fontFamily: 'var(--font-family)',
  fontSize: 14,
  fontSizeSM: 12,
  fontSizeLG: 16,
};

const lightToken = {
  colorPrimary: '#126DFF',
  colorSuccess: '#02B589',
  colorError: '#FC5D5D',
  colorInfo: '#126DFF',
  colorText: '#101828',
  colorTextSecondary: '#475467',
  colorTextTertiary: '#667085',
  colorTextQuaternary: '#98A2B3',
  colorBorder: '#DDE3EC',
  colorBorderSecondary: '#E9EDF3',
  colorBgContainer: '#FFFFFF',
  colorBgLayout: '#F5F7FB',
};

const darkToken = {
  colorPrimary: '#3E8BFF',
  colorSuccess: '#22C79D',
  colorError: '#FF6B6B',
  colorInfo: '#3E8BFF',
  colorText: '#E8ECF4',
  colorTextSecondary: '#B3BDCD',
  colorTextTertiary: '#8792A4',
  colorTextQuaternary: '#5F6B7D',
  colorBorder: '#2B3442',
  colorBorderSecondary: '#242C38',
  colorBgContainer: '#161C25',
  colorBgElevated: '#1C2330',
  colorBgLayout: '#12171F',
  colorBgSpotlight: '#1C2330',
};

// 模块级预建常量：同主题下引用稳定，ConfigProvider 无需重算派生 token。
// 「减弱动画」再乘二：antd 的 motion=false 只把自己的过渡时长归零，不碰 Spin / 按钮
// loading 的转圈——第三方动效交给第三方自己的开关，我们不去枚举人家的类名。
const LIGHT_THEME: ThemeConfig = {
  algorithm: antdTheme.defaultAlgorithm,
  token: { ...sharedToken, ...lightToken },
};
const DARK_THEME: ThemeConfig = {
  algorithm: antdTheme.darkAlgorithm,
  token: { ...sharedToken, ...darkToken },
};
const LIGHT_THEME_STILL: ThemeConfig = {
  ...LIGHT_THEME,
  token: { ...LIGHT_THEME.token, motion: false },
};
const DARK_THEME_STILL: ThemeConfig = {
  ...DARK_THEME,
  token: { ...DARK_THEME.token, motion: false },
};

export function getAppTheme(isDark: boolean, reducedMotion = false): ThemeConfig {
  if (isDark) return reducedMotion ? DARK_THEME_STILL : DARK_THEME;
  return reducedMotion ? LIGHT_THEME_STILL : LIGHT_THEME;
}
