/** `!command`: run in the user's shell in cwd, show the output, keep it as context for the next message. */
import { spawn, type ChildProcess } from 'node:child_process';
import { truncateToWidth, type Component } from '@mariozechner/pi-tui';
import { formatDuration } from './format.js';
import { theme } from './theme.js';

const MAX_CONTEXT_CHARS = 20_000;
const SHOWN_LINES = 20;

export class BashBlock implements Component {
  /** The latest output only: enough for the screen and for the next message's context. */
  private output = '';
  /** Lines dropped from the front so `output` stays bounded. */
  private droppedLines = 0;
  /** Rendered lines once the command has ended, by width. */
  private finished?: { width: number; lines: string[] };
  exitCode?: number | null;
  killed = false;
  readonly startedAt = Date.now();
  endedAt?: number;
  constructor(readonly command: string, readonly excluded: boolean) {}

  append(text: string): void {
    this.output += text;
    if (this.output.length > 2 * MAX_CONTEXT_CHARS) {
      const cut = this.output.length - MAX_CONTEXT_CHARS;
      this.droppedLines += this.output.slice(0, cut).split('\n').length - 1;
      this.output = this.output.slice(cut);
    }
  }

  render(width: number): string[] {
    if (this.finished?.width === width) return this.finished.lines;
    const running = this.endedAt === undefined;
    const status = running ? theme.accent('运行中')
      : this.killed ? theme.warning('已停止')
        : this.exitCode === 0 ? theme.success('✓') : theme.error(`退出码 ${this.exitCode}`);
    const time = formatDuration((this.endedAt ?? Date.now()) - this.startedAt);
    const note = this.excluded ? theme.dim(' · 不带入上下文') : '';
    const lines = ['', truncateToWidth(` ${theme.bashMode('$ ' + this.command)}  ${status} ${theme.dim(time)}${note}`, width)];
    const body = this.output.replace(/\s+$/, '').split('\n').filter((_, i, all) => all.length > 1 || all[0] !== '');
    const shownFrom = Math.max(0, body.length - SHOWN_LINES);
    const hidden = shownFrom + this.droppedLines;
    if (hidden) lines.push(truncateToWidth('   ' + theme.dim(`… 前面 ${hidden} 行省略`), width));
    for (const l of body.slice(shownFrom)) lines.push(truncateToWidth('   ' + theme.muted(l.replace(/\t/g, '  ')), width));
    if (!running) this.finished = { width, lines };
    return lines;
  }

  invalidate(): void {}

  /** What the agent sees with the next message. */
  contextText(): string {
    let out = this.output.replace(/\s+$/, '');
    if (out.length > MAX_CONTEXT_CHARS || this.droppedLines) out = '…（已截断）\n' + out.slice(-MAX_CONTEXT_CHARS);
    const code = this.killed ? '已被用户停止' : `退出码 ${this.exitCode}`;
    return `我在终端里执行了 \`${this.command}\`（${code}）：\n\`\`\`\n${out}\n\`\`\``;
  }
}

/** Strip colour and cursor escapes so command output stays plain text in the transcript and context. */
function plain(text: string): string {
  return text.replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, '').replace(/\x1b\][^\x07]*(\x07|\x1b\\)/g, '').replace(/\r(?!\n)/g, '\n');
}

export function runBash(block: BashBlock, cwd: string, onUpdate: () => void): { child: ChildProcess; done: Promise<void> } {
  const shell = process.env.SHELL || '/bin/sh';
  const child = spawn(shell, ['-c', block.command], { cwd, env: { ...process.env, TERM: 'dumb' }, stdio: ['ignore', 'pipe', 'pipe'] });
  const append = (chunk: Buffer) => {
    block.append(plain(chunk.toString('utf8')));
    onUpdate();
  };
  child.stdout?.on('data', append);
  child.stderr?.on('data', append);
  const done = new Promise<void>((resolve) => {
    child.on('error', (err) => {
      block.append(String(err.message));
      block.exitCode = 127;
      block.endedAt = Date.now();
      resolve();
    });
    child.on('close', (code) => {
      block.exitCode = code;
      block.endedAt = Date.now();
      resolve();
    });
  });
  return { child, done };
}
