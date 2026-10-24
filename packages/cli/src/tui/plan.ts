/** `plan_update` checklist: live above the editor while the run streams, frozen into the transcript at the end. */
import { truncateToWidth, type Component } from '@mariozechner/pi-tui';
import type { Plan } from './events.js';
import { theme } from './theme.js';

export class PlanView implements Component {
  constructor(public plan: Plan) {}

  render(width: number): string[] {
    const done = this.plan.steps.filter((s) => s.status === 'completed').length;
    const title = this.plan.title ? `计划 · ${this.plan.title}` : '计划';
    const lines = ['', truncateToWidth(` ${theme.bold(title)} ${theme.dim(`${done}/${this.plan.steps.length}`)}`, width)];
    for (const step of this.plan.steps) {
      const line = step.status === 'completed' ? theme.dim(`   ✓ ${step.title}`)
        : step.status === 'in_progress' ? theme.accent(`   ▸ ${step.title}`)
          : `   ${theme.muted('○')} ${step.title}`;
      lines.push(truncateToWidth(line, width));
    }
    return lines;
  }

  invalidate(): void {}
}
