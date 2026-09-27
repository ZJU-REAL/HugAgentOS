// ── ContentEditable helpers ─────────────────────────────────────────────

/** Extract plain text from editor, skipping chip spans. */
export function getEditorText(el: HTMLElement): string {
  let t = '';
  const walk = (n: Node) => {
    if (n.nodeType === Node.TEXT_NODE) {
      // Convert non-breaking spaces back to regular
      t += (n.textContent || '').replace(/\u00A0/g, ' ');
    } else if (n instanceof HTMLBRElement) {
      t += '\n';
    } else if (n instanceof HTMLElement) {
      if (n.dataset.chip) return; // skip chips
      const isBlock = n.tagName === 'DIV' || n.tagName === 'P';
      if (isBlock && t && !t.endsWith('\n')) t += '\n';
      for (const c of n.childNodes) walk(c);
    }
  };
  for (const c of el.childNodes) walk(c);
  return t;
}

/** Remove text backwards from cursor to the trigger char (@ or /). */
export function removeQueryAtCursor(_editor: HTMLElement, trigger: string) {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return;
  const range = sel.getRangeAt(0);
  const node = range.startContainer;
  if (node.nodeType !== Node.TEXT_NODE) return;
  const text = node.textContent || '';
  const cursor = range.startOffset;
  const idx = text.lastIndexOf(trigger, cursor - 1);
  if (idx === -1) return;
  node.textContent = text.slice(0, idx) + text.slice(cursor);
  try {
    range.setStart(node, idx);
    range.collapse(true);
    sel.removeAllRanges();
    sel.addRange(range);
  } catch { /* empty text node edge case */ }
}

/** Keep the trigger but clear its query, so a nested picker starts unfiltered. */
export function resetQueryAtCursor(_editor: HTMLElement, trigger: string) {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return;
  const range = sel.getRangeAt(0);
  const node = range.startContainer;
  if (node.nodeType !== Node.TEXT_NODE) return;
  const text = node.textContent || '';
  const cursor = range.startOffset;
  const idx = text.lastIndexOf(trigger, cursor - 1);
  if (idx === -1) return;
  node.textContent = text.slice(0, idx + trigger.length) + text.slice(cursor);
  try {
    range.setStart(node, idx + trigger.length);
    range.collapse(true);
    sel.removeAllRanges();
    sel.addRange(range);
  } catch { /* empty text node edge case */ }
}

/** 会话引用 chip 的前缀是个气泡图标（与 antd MessageOutlined 同一路径），
 *  其余几类用 @ / MCP 这样的字符前缀。 */
export const CHAT_CHIP_ICON =
  '<svg viewBox="64 64 896 896" width="1em" height="1em" fill="currentColor" aria-hidden="true">'
  + '<path d="M464 512a48 48 0 1096 0 48 48 0 10-96 0zm200 0a48 48 0 1096 0 48 48 0 10-96 0zm-400 0a48 48 0 1096 0 48 48 0 10-96 0zm661.2-173.6c-22.6-53.7-55-101.9-96.3-143.3a444.35 444.35 0 00-143.3-96.3C630.6 75.7 572.2 64 512 64h-2c-60.6.3-119.3 12.3-174.5 35.9a445.35 445.35 0 00-142 96.5c-40.9 41.3-73 89.3-95.2 142.8-23 55.4-34.6 114.3-34.3 174.9A449.4 449.4 0 00112 714v152a46 46 0 0046 46h152.1A449.4 449.4 0 00510 960h2.1c59.9 0 118-11.6 172.7-34.3a444.48 444.48 0 00142.8-95.2c41.3-40.9 73.8-88.7 96.5-142 23.6-55.2 35.6-113.9 35.9-174.5.3-60.9-11.5-120-34.8-175.6zm-151.1 438C704 845.8 611 884 512 884h-1.7c-60.3-.3-120.2-15.3-173.1-43.5l-8.4-4.5H188V695.2l-4.5-8.4C155.3 633.9 140.3 574 140 513.7c-.4-99.7 37.7-193.3 107.6-263.8 69.8-70.5 163.1-109.5 262.8-109.9h1.7c50 0 98.5 9.7 144.2 28.9 44.6 18.7 84.6 45.6 119 80 34.3 34.3 61.3 74.4 80 119 19.4 46.2 29.1 95.2 28.9 145.8-.6 99.6-39.7 192.9-110.1 262.7z"/></svg>';

/** Insert an inline chip span at the current cursor, followed by a space. */
export function insertChipAtCursor(
  editor: HTMLElement,
  prefix: string,
  name: string,
  cls: string,
  chipType?: string,
  chipId?: string,
  prefixIcon?: string,
) {
  clearEditorIfOnlyBrowserEmptyNodes(editor);

  const chip = document.createElement('span');
  chip.contentEditable = 'false';
  chip.className = `jx-editorChip ${cls}`;
  chip.dataset.chip = chipType || (prefix === '@' ? 'mention' : 'skill');
  chip.dataset.chipName = name;
  if (chipId) chip.dataset.chipId = chipId;
  const prefixEl = document.createElement('span');
  prefixEl.className = 'jx-editorChip-prefix';
  // 图标是本模块里的常量，名字来自用户数据——后者一律 textContent，
  // 不能拼进 innerHTML（会话标题是用户自己写的自由文本）。
  if (prefixIcon) prefixEl.innerHTML = prefixIcon;
  else prefixEl.textContent = prefix;
  const nameEl = document.createElement('span');
  nameEl.className = 'jx-editorChip-name';
  nameEl.textContent = name;
  chip.append(prefixEl, nameEl);

  const space = document.createTextNode('\u00A0');
  const sel = window.getSelection();
  if (sel && sel.rangeCount > 0 && editor.contains(sel.getRangeAt(0).commonAncestorContainer)) {
    const range = sel.getRangeAt(0);
    range.collapse(true);
    const fragment = document.createDocumentFragment();
    fragment.append(chip, space);
    range.insertNode(fragment);
  } else {
    editor.appendChild(chip);
    editor.appendChild(space);
  }
  setCaretAfter(space);
}

/** Insert a chip before the prefilled text used by plugin-first entry points. */
export function insertChipAtStart(editor: HTMLElement, prefix: string, name: string, cls: string, chipType?: string) {
  const selection = window.getSelection();
  if (selection) {
    const range = document.createRange();
    range.setStart(editor, 0);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
  }
  insertChipAtCursor(editor, prefix, name, cls, chipType);
}

/** 每个 chip 后面跟着插入时补的一个不换行空格，删 chip 要把它一起带走。 */
const CHIP_TRAILING_SPACE = String.fromCharCode(0xa0);

/** Remove every chip of one type, along with the trailing space its insertion added.
 *  Returns whether anything was removed, so callers only re-sync when the DOM changed. */
export function removeChipsOfType(editor: HTMLElement, chipType: string): boolean {
  const stale = editor.querySelectorAll(`[data-chip="${chipType}"]`);
  if (!stale.length) return false;
  stale.forEach((el) => {
    const next = el.nextSibling;
    if (next?.nodeType === Node.TEXT_NODE && next.textContent?.startsWith(CHIP_TRAILING_SPACE)) {
      next.textContent = next.textContent.slice(1);
      if (!next.textContent) next.remove();
    }
    el.remove();
  });
  return true;
}

export function setEditorPlainText(editor: HTMLElement, text: string) {
  editor.innerHTML = '';
  if (text) {
    editor.textContent = text;
  }
}

export function moveCaretToEnd(editor: HTMLElement) {
  const selection = window.getSelection();
  if (!selection) return;
  const range = document.createRange();
  range.selectNodeContents(editor);
  range.collapse(false);
  selection.removeAllRanges();
  selection.addRange(range);
}

function setCaretAfter(node: Node) {
  const selection = window.getSelection();
  if (!selection) return;
  const range = document.createRange();
  range.setStartAfter(node);
  range.collapse(true);
  selection.removeAllRanges();
  selection.addRange(range);
}

function clearEditorIfOnlyBrowserEmptyNodes(editor: HTMLElement) {
  if (editor.querySelector('[data-chip]')) return;
  if (getEditorText(editor).trim()) return;
  if (editor.childNodes.length > 0) {
    editor.replaceChildren();
  }
}
