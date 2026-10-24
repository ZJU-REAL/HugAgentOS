/** Slash commands, their autocomplete, and `@file` completion (local mode only). */
import { execFile } from 'node:child_process';
import { readdir } from 'node:fs/promises';
import { basename, join, relative } from 'node:path';
import {
  CombinedAutocompleteProvider, fuzzyFilter,
  type AutocompleteItem, type AutocompleteProvider, type AutocompleteSuggestions, type SlashCommand,
} from '@mariozechner/pi-tui';

export interface CommandSpec { name: string; description: string; argumentHint?: string; args?: string[] }

export const COMMANDS: CommandSpec[] = [
  { name: 'mode', description: '切换执行位置', argumentHint: '[local|cloud|device:<id>]', args: ['local', 'cloud', 'device:'] },
  { name: 'model', description: '选择模型（对之后的消息生效）' },
  { name: 'new', description: '开始新会话' },
  { name: 'resume', description: '恢复之前的会话' },
  { name: 'fork', description: '从当前会话分叉出新会话' },
  { name: 'compact', description: '立即压缩当前会话上下文' },
  { name: 'permissions', description: '工具审批档位', argumentHint: '[ask|auto|full]', args: ['ask', 'auto', 'full'] },
  { name: 'copy', description: '复制最后一条回答到剪贴板' },
  { name: 'hotkeys', description: '快捷键一览' },
  { name: 'help', description: '命令一览' },
  { name: 'quit', description: '退出' },
];

export interface ParsedCommand { name: string; args: string }

/** `/name args` → parsed; anything else (including `/path/like/this`) → null. */
export function parseCommand(text: string): ParsedCommand | null {
  const match = /^\/([a-z][\w-]*)(?:\s+([\s\S]*))?$/i.exec(text.trim());
  if (!match) return null;
  const name = match[1].toLowerCase();
  return COMMANDS.some((c) => c.name === name) ? { name, args: (match[2] ?? '').trim() } : null;
}

function slashCommands(): SlashCommand[] {
  return COMMANDS.map((c) => ({
    name: c.name,
    description: c.description,
    argumentHint: c.argumentHint,
    getArgumentCompletions: c.args
      ? (prefix: string) => {
        // A fully typed argument needs no list, so Enter submits instead of re-applying it.
        const items = c.args!.filter((a) => a.startsWith(prefix) && a !== prefix).map((a) => ({ value: a, label: a }));
        return items.length ? items : null;
      }
      : undefined,
  }));
}

/** Candidate files for `@`: git's view of the tree when available, else a bounded walk. */
async function listFiles(cwd: string, limit = 5000): Promise<string[]> {
  const fromGit = await new Promise<string[] | null>((resolve) => {
    execFile('git', ['ls-files', '--cached', '--others', '--exclude-standard'], { cwd, maxBuffer: 32 * 1024 * 1024 }, (err, stdout) => {
      resolve(err ? null : stdout.split('\n').filter(Boolean).slice(0, limit));
    });
  });
  if (fromGit) return fromGit;
  const out: string[] = [];
  const skip = new Set(['node_modules', '.git', 'dist', 'build', '.venv', '__pycache__']);
  const queue = [cwd];
  while (queue.length && out.length < limit) {
    const dir = queue.shift()!;
    let entries;
    try { entries = await readdir(dir, { withFileTypes: true }); } catch { continue; }
    for (const e of entries) {
      if (e.name.startsWith('.') && e.name !== '.github') continue;
      const full = join(dir, e.name);
      if (e.isDirectory()) { if (!skip.has(e.name)) queue.push(full); }
      else out.push(relative(cwd, full));
    }
  }
  return out;
}

/** Wraps pi's provider: slash commands as-is; `@` fuzzy files from our own list (no `fd` needed). */
export class AppAutocomplete implements AutocompleteProvider {
  private inner: CombinedAutocompleteProvider;
  private files?: Promise<string[]>;
  private filesAt = 0;

  constructor(private cwd: string, private canAccessCwd: () => boolean) {
    this.inner = new CombinedAutocompleteProvider(slashCommands(), cwd);
  }

  private atPrefix(line: string, col: number): string | null {
    const before = line.slice(0, col);
    const match = /(?:^|[\s(])(@[^\s]*)$/.exec(before);
    return match ? match[1] : null;
  }

  private fileList(): Promise<string[]> {
    if (!this.files || Date.now() - this.filesAt > 30_000) {
      this.filesAt = Date.now();
      this.files = listFiles(this.cwd);
    }
    return this.files;
  }

  async getSuggestions(lines: string[], cursorLine: number, cursorCol: number, options: { signal: AbortSignal; force?: boolean }): Promise<AutocompleteSuggestions | null> {
    const prefix = this.atPrefix(lines[cursorLine] ?? '', cursorCol);
    if (prefix !== null) {
      if (!this.canAccessCwd()) return null;
      const query = prefix.slice(1);
      const files = await this.fileList();
      if (options.signal.aborted) return null;
      const matches = query ? fuzzyFilter(files, query, (f) => f) : files;
      const items: AutocompleteItem[] = matches.slice(0, 20).map((f) => ({ value: '@' + f, label: basename(f), description: f }));
      return items.length ? { items, prefix } : null;
    }
    if (!this.canAccessCwd() && !(lines[cursorLine] ?? '').startsWith('/')) return null;
    return this.inner.getSuggestions(lines, cursorLine, cursorCol, options);
  }

  applyCompletion(lines: string[], cursorLine: number, cursorCol: number, item: AutocompleteItem, prefix: string) {
    return this.inner.applyCompletion(lines, cursorLine, cursorCol, item, prefix);
  }

  shouldTriggerFileCompletion(lines: string[], cursorLine: number, cursorCol: number): boolean {
    if (!this.canAccessCwd()) return false;
    return this.inner.shouldTriggerFileCompletion(lines, cursorLine, cursorCol);
  }
}

export const HELP_TEXT = COMMANDS.map((c) => `/${c.name}${c.argumentHint ? ' ' + c.argumentHint : ''}  —  ${c.description}`).join('\n')
  + '\n!命令  —  在本机当前目录执行（仅 local），输出会带进下一条消息；!!命令 不带入'
  + '\n@路径  —  引用当前目录下的文件（仅 local）';

export const HOTKEYS_TEXT = [
  'enter          发送；运行中 = 插话（steer）',
  'alt+enter      运行中 = 排到本轮之后（follow-up）',
  'shift+enter    换行',
  'esc            中断运行 / 关闭补全 / 停止 !命令',
  'ctrl+c         清空输入；1 秒内按两次退出',
  'ctrl+d         输入为空时退出',
  'ctrl+t         展开 / 折叠思考',
  'ctrl+o         展开 / 折叠工具详情',
  '↑ / ↓          历史输入',
  'tab            补全路径与命令',
].join('\n');
