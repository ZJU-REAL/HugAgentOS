/**
 * 主题模式唯一真源：system / light / dark 三档。
 * - 持久化用全局 key（不做 userScopedKey）：防闪烁内联脚本在登录前运行，必须能直接读到；
 *   index.html 里的 boot script 与本文件读同一个 key、同一套解析规则，两边同步改。
 * - DOM 落地：documentElement 上打 data-theme="dark" + colorScheme（原生表单控件/滚动条跟随）。
 *   CSS 侧由 variables.css 的 :root[data-theme="dark"] 覆盖块接管。
 */

import { writeLocal } from './storage';

export type ThemeMode = 'system' | 'light' | 'dark';

const THEME_STORAGE_KEY = 'hugagent_theme_mode';

export function loadThemeMode(): ThemeMode {
  if (typeof window === 'undefined') return 'system';
  try {
    const raw = window.localStorage.getItem(THEME_STORAGE_KEY);
    return raw === 'light' || raw === 'dark' ? raw : 'system';
  } catch {
    return 'system';
  }
}

export function saveThemeMode(mode: ThemeMode): void {
  try {
    writeLocal(THEME_STORAGE_KEY, mode);
  } catch {
    /* storage 不可用时静默降级为会话内生效 */
  }
}

export function systemPrefersDark(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: dark)').matches
  );
}

export function applyThemeToDom(isDark: boolean): void {
  // 幂等短路：boot 脚本通常已落好状态，挂载时避免多余的文档级样式失效
  const rootEl = document.documentElement;
  const hasDark = rootEl.getAttribute('data-theme') === 'dark';
  if (isDark !== hasDark) {
    if (isDark) rootEl.setAttribute('data-theme', 'dark');
    else rootEl.removeAttribute('data-theme');
  }
  const scheme = isDark ? 'dark' : 'light';
  if (rootEl.style.colorScheme !== scheme) rootEl.style.colorScheme = scheme;
}

/** 监听系统外观变化；返回取消函数。非浏览器环境（SSR/测试）为 no-op。 */
/** 系统是否开启「减弱动画」（Windows 节电模式会自动开启）。 */
export function systemPrefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

export function watchReducedMotion(onChange: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {};
  const media = window.matchMedia('(prefers-reduced-motion: reduce)');
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
}

export function watchSystemTheme(onChange: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {};
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
}
