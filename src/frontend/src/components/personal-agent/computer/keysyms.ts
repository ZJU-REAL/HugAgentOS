// X11 keysyms for browser keyboard events, sent to the cloud desktop over VNC.

const SPECIAL: Record<string, number> = {
  Backspace: 0xff08, Tab: 0xff09, Enter: 0xff0d, Escape: 0xff1b, Delete: 0xffff, Insert: 0xff63,
  Home: 0xff50, ArrowLeft: 0xff51, ArrowUp: 0xff52, ArrowRight: 0xff53, ArrowDown: 0xff54,
  PageUp: 0xff55, PageDown: 0xff56, End: 0xff57, CapsLock: 0xffe5, ContextMenu: 0xff67,
  Shift: 0xffe1, Control: 0xffe3, Alt: 0xffe9, AltGraph: 0xfe03,
  // Command on macOS acts as Control on the Linux desktop, so Cmd+C copies and Cmd+V pastes.
  Meta: 0xffe3,
};
const RIGHT: Record<string, number> = { Shift: 0xffe2, Control: 0xffe4, Alt: 0xffea, Meta: 0xffe4 };

export const MODIFIERS = new Set(['Shift', 'Control', 'Alt', 'Meta', 'AltGraph']);
export const CONTROL = SPECIAL.Control;
export const ALT = SPECIAL.Alt;
export const CONTROL_KEYS = new Set([SPECIAL.Control, RIGHT.Control]);
export const ALT_KEYS = new Set([SPECIAL.Alt, RIGHT.Alt]);

/** The keysym for one character. Latin-1 maps directly; other characters use Unicode keysyms. */
export function charKeysym(char: string): number {
  if (char === '\n') return SPECIAL.Enter;
  if (char === '\t') return SPECIAL.Tab;
  const codepoint = char.codePointAt(0)!;
  return codepoint >= 0x20 && codepoint <= 0xff ? codepoint : 0x01000000 + codepoint;
}

/** The keysym for a key event, or null when the key has no desktop meaning. */
export function eventKeysym(event: KeyboardEvent): number | null {
  const { key } = event;
  if (event.location === 2 && RIGHT[key]) return RIGHT[key];
  if (SPECIAL[key]) return SPECIAL[key];
  const fn = /^F([1-9]|1[0-2])$/.exec(key);
  if (fn) return 0xffbe + Number(fn[1]) - 1;
  return [...key].length === 1 ? charKeysym(key) : null;
}
