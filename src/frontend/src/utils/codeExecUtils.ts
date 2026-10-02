/**
 * Shared utilities for code-related UI components.
 * Used by CodeView and file-size displays (myspace / config).
 */

import { highlightSyntax } from './syntaxHighlight';

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
  return highlightSyntax(code, lang);
}
