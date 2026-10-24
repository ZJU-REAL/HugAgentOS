/** Status bar under the editor: where the agent runs, model, permissions, context fill, run state. */
import { truncateToWidth, visibleWidth, type Component } from '@mariozechner/pi-tui';
import type { EngineInfo, PermissionMode } from '../engine/engine.js';
import { formatDuration } from './format.js';
import { theme } from './theme.js';

export const PERMISSION_LABELS: Record<PermissionMode, string> = { ask: '审批:按需', auto: '审批:自动', full: '审批:完全放开' };

export interface FooterState {
  info: EngineInfo;
  model?: string;
  permission?: PermissionMode;
  contextPercent?: number;
  /** Set while a run streams. */
  runStartedAt?: number;
  /** Short state word shown instead of the timer, e.g. "等待确认". */
  runState?: string;
  sessionTitle?: string;
}

export class Footer implements Component {
  constructor(public state: FooterState) {}

  update(patch: Partial<FooterState>): void { this.state = { ...this.state, ...patch }; }

  render(width: number): string[] {
    const s = this.state;
    const title = s.sessionTitle ? ` • ${s.sessionTitle}` : '';
    let where = theme.dim(s.info.label + title);
    if (!s.info.canAccessCwd) {
      // "cloud · 无本地文件访问" stays in view the whole time: the agent cannot touch this machine.
      const rest = s.info.label.startsWith(s.info.mode + ' · ') ? s.info.label.slice(s.info.mode.length + 3) : s.info.label;
      where = theme.warning(`${s.info.mode} · 无本地文件访问`) + theme.dim(` · ${rest}${title}`);
    }
    const left: string[] = [theme.dim(s.model || '默认模型')];
    if (s.permission) left.push(theme.dim(PERMISSION_LABELS[s.permission]));
    if (s.contextPercent !== undefined) {
      const pct = `ctx ${Math.round(s.contextPercent)}%`;
      left.push(s.contextPercent > 90 ? theme.error(pct) : s.contextPercent > 70 ? theme.warning(pct) : theme.dim(pct));
    }
    const leftText = left.join(theme.dim(' · '));
    const right = s.runStartedAt !== undefined
      ? theme.accent(s.runState ?? `运行中 ${formatDuration(Date.now() - s.runStartedAt)}`)
      : theme.dim(s.runState ?? '空闲');
    const gap = width - visibleWidth(leftText) - visibleWidth(right);
    const stats = gap >= 2 ? leftText + ' '.repeat(gap) + right : truncateToWidth(leftText + '  ' + right, width);
    return [truncateToWidth(where, width), stats];
  }

  invalidate(): void {}
}
