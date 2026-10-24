/** Colours and component themes, taken from pi's dark theme so the TUI reads like pi. */
import chalk from 'chalk';
import type { EditorTheme, MarkdownTheme, SelectListTheme } from '@mariozechner/pi-tui';

const hex = (color: string) => chalk.hex(color);

export const theme = {
  accent: hex('#8abeb7'),
  border: hex('#5f87ff'),
  borderMuted: hex('#505050'),
  success: hex('#b5bd68'),
  error: hex('#cc6666'),
  warning: hex('#ffff00'),
  muted: hex('#808080'),
  dim: hex('#666666'),
  thinking: hex('#808080'),
  bashMode: hex('#b5bd68'),
  userBg: chalk.bgHex('#343541'),
  bold: (text: string) => chalk.bold(text),
  italic: (text: string) => chalk.italic(text),
};

export const markdownTheme: MarkdownTheme = {
  heading: hex('#f0c674'),
  link: hex('#81a2be'),
  linkUrl: theme.dim,
  code: theme.accent,
  codeBlock: theme.success,
  codeBlockBorder: theme.muted,
  quote: theme.muted,
  quoteBorder: theme.muted,
  hr: theme.muted,
  listBullet: theme.accent,
  bold: (text) => chalk.bold(text),
  italic: (text) => chalk.italic(text),
  strikethrough: (text) => chalk.strikethrough(text),
  underline: (text) => chalk.underline(text),
};

export const selectListTheme: SelectListTheme = {
  selectedPrefix: theme.accent,
  selectedText: theme.accent,
  description: theme.muted,
  scrollInfo: theme.muted,
  noMatch: theme.muted,
};

export const editorTheme: EditorTheme = {
  borderColor: theme.borderMuted,
  selectList: selectListTheme,
};
