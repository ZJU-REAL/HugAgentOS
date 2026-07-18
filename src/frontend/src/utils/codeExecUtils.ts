/**
 * Shared utilities for code-related UI components.
 * Used by CodeView and file-size displays (myspace / config).
 */

import hljs from 'highlight.js';

export const LANG_LABELS: Record<string, string> = {
  python: 'Python',
  javascript: 'JavaScript',
  bash: 'Bash',
  sh: 'Shell',
};

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** Syntax-highlight code using highlight.js. Returns HTML string. */
export function highlightCode(code: string, language: string): string {
  if (!code) return '';
  const lang = language === 'sh' ? 'bash' : language;
  if (hljs.getLanguage(lang)) {
    try {
      return hljs.highlight(code, { language: lang }).value;
    } catch { /* fallback */ }
  }
  return code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
