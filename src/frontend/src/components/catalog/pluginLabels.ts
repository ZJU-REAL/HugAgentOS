export function sourceLabel(source?: string): string | null {
  if (source === 'imported_claude') return 'Claude Code';
  if (source === 'imported_codex') return 'Codex';
  return null;
}
