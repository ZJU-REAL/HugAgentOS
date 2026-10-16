import type { RefObject } from 'react';
import { richEditor } from '../chat/composerRichText';

/** An edited/submitted/replaced document invalidates the captured cursor. */
export function voiceAnchor(input: RefObject<HTMLElement | null>): (text: string) => boolean {
  const element = input.current;
  const editor = element && richEditor(element);
  if (!editor) return () => false;
  const document = editor.state.doc;
  const { from, to } = editor.state.selection;
  return text => {
    if (editor.isDestroyed || input.current !== element || editor.state.doc !== document) return false;
    // Match Claude's boundary spacing and leave the cursor before the suffix.
    const prefix = document.textBetween(0, from, '\n');
    const suffix = document.textBetween(to, document.content.size, '\n');
    const leading = prefix.length > 0 && !/\s$/.test(prefix) && text.length > 0 ? ' ' : '';
    const trailing = suffix.length > 0 && !/^\s/.test(suffix) && text.length > 0 ? ' ' : '';
    return editor.chain().focus().insertContentAt({ from, to }, { type: 'text', text: leading + text + trailing })
      .setTextSelection(from + leading.length + text.length).run();
  };
}
