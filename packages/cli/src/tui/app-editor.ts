/** pi-tui Editor plus the app-level keys, the way pi's CustomEditor does it. */
import { Editor, matchesKey, type EditorOptions, type EditorTheme, type TUI } from '@mariozechner/pi-tui';

export interface EditorActions {
  escape(): void;
  ctrlC(): void;
  /** Ctrl+D on an empty editor. */
  exit(): void;
  toggleThinking(): void;
  toggleTools(): void;
  /** Alt+Enter: returns true when it queued the text (only while a run streams). */
  followUp(text: string): boolean;
}

export class AppEditor extends Editor {
  actions?: EditorActions;

  constructor(tui: TUI, theme: EditorTheme, options?: EditorOptions) {
    super(tui, theme, options);
  }

  override handleInput(data: string): void {
    const a = this.actions;
    if (!a) return super.handleInput(data);
    if (matchesKey(data, 'escape')) {
      if (this.isShowingAutocomplete()) return super.handleInput(data);
      return a.escape();
    }
    if (matchesKey(data, 'ctrl+c')) return a.ctrlC();
    if (matchesKey(data, 'ctrl+d') && this.getText().length === 0) return a.exit();
    if (matchesKey(data, 'ctrl+t')) return a.toggleThinking();
    if (matchesKey(data, 'ctrl+o')) return a.toggleTools();
    if (matchesKey(data, 'alt+enter') && !this.isShowingAutocomplete()) {
      const text = this.getExpandedText().trim();
      if (text && a.followUp(text)) {
        this.addToHistory(text);
        this.setText('');
        return;
      }
    }
    super.handleInput(data);
  }
}
