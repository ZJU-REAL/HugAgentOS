/**
 * `user_question` answered inline, one question at a time: pick an option (space toggles on
 * multi-select), type your own answer, or skip. Collected answers go to `engine.answer`.
 */
import { Input, matchesKey, truncateToWidth, wrapTextWithAnsi, type Component, type Focusable } from '@mariozechner/pi-tui';
import type { QuestionAnswer } from '../../engine/engine.js';
import type { Question, QuestionRequest } from '../events.js';
import { theme } from '../theme.js';

const CUSTOM = '__custom__';
const SKIP = '__skip__';

export class QuestionPrompt implements Component, Focusable {
  private index = 0;
  private cursor = 0;
  private checked = new Set<string>();
  private answers: QuestionAnswer[] = [];
  private input = new Input();
  private typing = false;
  private _focused = false;

  constructor(private request: QuestionRequest, private done: (answers: QuestionAnswer[]) => void) {
    this.input.onSubmit = (value) => this.submitCustom(value);
    this.input.onEscape = () => { this.typing = false; };
    this.preselect();
  }

  get focused(): boolean { return this._focused; }
  set focused(value: boolean) {
    this._focused = value;
    this.input.focused = value && this.typing;
  }

  get requestId(): string { return this.request.requestId; }
  private get question(): Question { return this.request.questions[this.index]; }

  private rows(): { value: string; label: string; description?: string }[] {
    return [
      ...this.question.options.map((o) => ({ value: o.id, label: o.label + (o.recommended ? theme.success(' 推荐') : ''), description: o.description })),
      { value: CUSTOM, label: '自己输入…' },
      { value: SKIP, label: '跳过' },
    ];
  }

  private preselect(): void {
    const rec = this.question.options.findIndex((o) => o.recommended);
    this.cursor = rec >= 0 ? rec : 0;
    this.checked.clear();
  }

  private next(answer: QuestionAnswer): void {
    this.answers.push(answer);
    this.typing = false;
    this.input.setValue('');
    this.input.focused = false;
    if (this.index + 1 >= this.request.questions.length) return this.done(this.answers);
    this.index += 1;
    this.preselect();
  }

  private submitCustom(value: string): void {
    const custom = value.trim();
    if (!custom) return;
    this.next({ id: this.question.id, selected: this.question.multiSelect ? [...this.checked] : [], custom });
  }

  /** Esc on the list: skip this and every remaining question. */
  skipAll(): void {
    for (let i = this.index; i < this.request.questions.length; i++) {
      this.answers.push({ id: this.request.questions[i].id, selected: [], skipped: true });
    }
    this.done(this.answers);
  }

  handleInput(data: string): void {
    if (this.typing) {
      if (matchesKey(data, 'escape')) {
        this.typing = false;
        this.input.focused = false;
        return;
      }
      this.input.handleInput(data);
      return;
    }
    const rows = this.rows();
    if (matchesKey(data, 'up')) this.cursor = (this.cursor - 1 + rows.length) % rows.length;
    else if (matchesKey(data, 'down') || matchesKey(data, 'tab')) this.cursor = (this.cursor + 1) % rows.length;
    else if (matchesKey(data, 'escape') || matchesKey(data, 'ctrl+c')) this.skipAll();
    else if (/^[1-9]$/.test(data) && Number(data) <= rows.length) {
      this.cursor = Number(data) - 1;
      this.choose(rows[this.cursor].value, false);
    } else if (matchesKey(data, 'space') && this.question.multiSelect) this.toggle(rows[this.cursor].value);
    else if (matchesKey(data, 'enter')) this.choose(rows[this.cursor].value, true);
  }

  private toggle(value: string): void {
    if (value === CUSTOM || value === SKIP) return;
    if (this.checked.has(value)) this.checked.delete(value);
    else this.checked.add(value);
  }

  private choose(value: string, enter: boolean): void {
    if (value === SKIP) return this.next({ id: this.question.id, selected: [], skipped: true });
    if (value === CUSTOM) {
      this.typing = true;
      this.input.focused = this._focused;
      return;
    }
    if (this.question.multiSelect) {
      if (!enter) return this.toggle(value);
      if (!this.checked.size) this.checked.add(value);
      return this.next({ id: this.question.id, selected: [...this.checked] });
    }
    this.next({ id: this.question.id, selected: [value] });
  }

  render(width: number): string[] {
    const q = this.question;
    const border = theme.warning('─'.repeat(Math.max(1, width)));
    const total = this.request.questions.length;
    const head = `? ${q.header ? q.header + ' · ' : ''}${total > 1 ? `(${this.index + 1}/${total}) ` : ''}`;
    const lines = [border];
    for (const l of wrapTextWithAnsi(theme.bold(head + q.question), Math.max(10, width - 2))) lines.push(truncateToWidth(' ' + l, width));
    if (q.description) {
      for (const l of wrapTextWithAnsi(theme.muted(q.description), Math.max(10, width - 2))) lines.push(truncateToWidth(' ' + l, width));
    }
    lines.push('');
    this.rows().forEach((row, i) => {
      const selected = i === this.cursor;
      const box = q.multiSelect && row.value !== CUSTOM && row.value !== SKIP ? (this.checked.has(row.value) ? '[x] ' : '[ ] ') : '';
      const prefix = selected ? theme.accent('→ ') : '  ';
      const label = `${i + 1}. ${box}${row.label}`;
      const desc = row.description ? theme.muted('  ' + row.description) : '';
      lines.push(truncateToWidth(prefix + (selected ? theme.accent(label) : label) + desc, width));
    });
    if (this.typing) {
      lines.push('');
      lines.push(...this.input.render(Math.max(1, width - 2)).map((l) => truncateToWidth('  ' + l, width)));
    }
    lines.push('');
    const hint = this.typing ? 'enter 提交 · esc 返回选项'
      : q.multiSelect ? '↑↓ 移动 · space 勾选 · enter 确认 · esc 全部跳过' : '↑↓ 选择 · enter 确认 · 数字直选 · esc 全部跳过';
    lines.push(truncateToWidth(' ' + theme.dim(hint), width));
    lines.push(border);
    return lines;
  }

  invalidate(): void { this.input.invalidate(); }
}
