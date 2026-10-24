/** A bordered pick-one list that takes the editor's place, like pi's selectors. */
import { SelectList, truncateToWidth, wrapTextWithAnsi, type Component, type SelectItem } from '@mariozechner/pi-tui';
import { selectListTheme, theme } from '../theme.js';

export interface TitledSelectOptions {
  title: string;
  /** Extra lines under the title (wrapped). */
  body?: string;
  items: SelectItem[];
  initialValue?: string;
  hint?: string;
  maxVisible?: number;
  onSelect: (item: SelectItem) => void;
  onCancel: () => void;
  /** Accent the border (things waiting on the user). */
  attention?: boolean;
}

export class TitledSelect implements Component {
  readonly list: SelectList;
  constructor(private options: TitledSelectOptions) {
    this.list = new SelectList(options.items, options.maxVisible ?? 8, selectListTheme, { maxPrimaryColumnWidth: 48 });
    const index = options.initialValue ? options.items.findIndex((i) => i.value === options.initialValue) : -1;
    if (index >= 0) this.list.setSelectedIndex(index);
    this.list.onSelect = options.onSelect;
    this.list.onCancel = options.onCancel;
  }

  handleInput(data: string): void { this.list.handleInput(data); }

  render(width: number): string[] {
    const border = (this.options.attention ? theme.warning : theme.border)('─'.repeat(Math.max(1, width)));
    const lines = [border, truncateToWidth(' ' + theme.bold(this.options.title), width)];
    if (this.options.body) {
      for (const raw of this.options.body.split('\n')) {
        for (const l of wrapTextWithAnsi(theme.muted(raw), Math.max(10, width - 2))) lines.push(truncateToWidth(' ' + l, width));
      }
    }
    lines.push('');
    lines.push(...this.list.render(width));
    lines.push('');
    lines.push(truncateToWidth(' ' + theme.dim(this.options.hint ?? '↑↓ 选择 · enter 确认 · esc 取消'), width));
    lines.push(border);
    return lines;
  }

  invalidate(): void { this.list.invalidate(); }
}
