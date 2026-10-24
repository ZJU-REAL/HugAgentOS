/** Copy via OSC 52 (works over SSH in most terminals), plus pbcopy on macOS where Terminal.app ignores OSC 52. */
import { spawn } from 'node:child_process';

export function osc52(text: string): string {
  return `\x1b]52;c;${Buffer.from(text, 'utf8').toString('base64')}\x07`;
}

export function copyToClipboard(text: string, write: (data: string) => void): void {
  write(osc52(text));
  if (process.platform === 'darwin' && !process.env.SSH_TTY) {
    try {
      const child = spawn('pbcopy', [], { stdio: ['pipe', 'ignore', 'ignore'] });
      child.on('error', () => {});
      child.stdin.end(text);
    } catch {
      // OSC 52 already sent.
    }
  }
}
