// HugAgentOS Ant Design tokens v2 — reference for src/frontend/src/appTheme.ts.
// Differences from the current file: hover/active darken instead of lighten; dark primary stays #126DFF;
// semantic text colours and placeholder meet 4.5:1.
import type { ThemeConfig } from 'antd';
import { theme as antdTheme } from 'antd';

const shared = {
  borderRadius: 10, borderRadiusLG: 16, borderRadiusSM: 6,
  fontFamily: 'var(--font-family)', fontSize: 14, fontSizeSM: 12, fontSizeLG: 16,
  colorWarning: '#F8AB42',
};

export const LIGHT_V2: ThemeConfig = {
  algorithm: antdTheme.defaultAlgorithm,
  token: {
    ...shared,
    colorPrimary: '#126DFF', colorPrimaryHover: '#0F62E6', colorPrimaryActive: '#0A56CC',
    colorLink: '#0F62E6', colorInfo: '#126DFF',
    colorSuccess: '#02B589', colorError: '#D92D20',
    colorSuccessText: '#067A5E', colorWarningText: '#B45309', colorErrorText: '#D92D20',
    colorText: '#101828', colorTextSecondary: '#475467', colorTextTertiary: '#667085', colorTextPlaceholder: '#667085',
    colorBorder: '#C5CCD8', colorBorderSecondary: '#E3E8EF',
    colorBgContainer: '#FFFFFF', colorBgLayout: '#F5F7FB', colorBgElevated: '#FFFFFF',
  },
};

export const DARK_V2: ThemeConfig = {
  algorithm: antdTheme.darkAlgorithm,
  token: {
    ...shared,
    colorPrimary: '#126DFF', colorPrimaryHover: '#0F62E6', colorPrimaryActive: '#0A56CC',
    colorLink: '#5C9BFF', colorInfo: '#126DFF',
    colorSuccess: '#22C79D', colorError: '#FF6B6B',
    colorSuccessText: '#3DDBB0', colorWarningText: '#FFC26B', colorErrorText: '#FF8A80',
    colorText: '#E8ECF4', colorTextSecondary: '#B3BDCD', colorTextTertiary: '#8792A4', colorTextPlaceholder: '#8792A4',
    colorBorder: '#3A4556', colorBorderSecondary: '#2B3442',
    colorBgContainer: '#161C25', colorBgLayout: '#12171F', colorBgElevated: '#1C2330', colorBgSpotlight: '#1C2330',
  },
};
