/** Automation ownership is metadata; the visible title does not need a textual label. */
export function conversationTitle(title: string): string {
  return title.replace(/^\[自动化\]\s*/, '');
}
