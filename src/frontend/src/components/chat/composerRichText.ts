import { Editor } from '@tiptap/core';
import DOMPurify from 'dompurify';
import { closeHistory } from '@tiptap/pm/history';
import { DOMSerializer } from '@tiptap/pm/model';
import { composerExtensions } from './composerExtensions';
import { insertClipboardContent } from './composerClipboard';

const editors = new WeakMap<HTMLElement, Editor>();
export function richEditor(element: HTMLElement): Editor | undefined { return editors.get(element); }

export function createRichEditor(element: HTMLElement, onUpdate: () => void): Editor {
  const editor = new Editor({
    element,
    // Clipboard parsing owns formatting; running paste rules again would double-parse it.
    enablePasteRules: false,
    extensions: composerExtensions(),
    editorProps: {
      attributes: { class: 'jx-composer jx-composerEditor jx-richComposer', role: 'textbox', 'aria-multiline': 'true' },
      handlePaste(_view, event) {
        const data = event.clipboardData;
        if (!data) return false;
        // Images stay on the existing attachment path, handled by the composer.
        if (Array.from(data.files).some(file => file.type.startsWith('image/'))
          || Array.from(data.items).some(item => item.kind === 'file' && item.type.startsWith('image/'))) return false;
        event.preventDefault();
        insertClipboardContent(editor, data);
        return true;
      },
      handleDOMEvents: {
        copy(view, event) {
          const slice = view.state.selection.content();
          if (!event.clipboardData || view.state.selection.empty) return false;
          event.preventDefault();
          const json = { type: 'doc', content: slice.content.toJSON() };
          const fragment = DOMSerializer.fromSchema(view.state.schema).serializeFragment(slice.content);
          const wrapper = document.createElement('div');
          wrapper.append(fragment);
          wrapper.querySelectorAll('[data-chip]').forEach(node => node.remove());
          event.clipboardData.setData('text/plain', editor.markdown!.serialize(json));
          event.clipboardData.setData('text/html', DOMPurify.sanitize(wrapper.innerHTML));
          return true;
        },
      },
    },
    onUpdate,
  });
  editors.set(editor.view.dom, editor);
  return editor;
}
export function disposeRichEditor(editor: Editor) {
  editors.delete(editor.view.dom);
  editor.destroy();
}
export function insertRichChip(editor: Editor, attrs: Record<string, string>, atStart = false) {
  const position = atStart ? 1 : editor.state.selection.from;
  editor.chain().insertContentAt(position, { type: 'invocationChip', attrs }).focus().run();
}
export function removeRichChips(editor: Editor, type: string): boolean {
  const positions: Array<{ from: number; to: number }> = [];
  editor.state.doc.descendants((node, pos) => {
    if (node.type.name === 'invocationChip' && node.attrs.chip === type) positions.push({ from: pos, to: pos + node.nodeSize });
  });
  if (!positions.length) return false;
  const transaction = editor.state.tr;
  positions.reverse().forEach(({ from, to }) => transaction.delete(from, to));
  editor.view.dispatch(closeHistory(transaction));
  return true;
}
export function removeRichQuery(editor: Editor, trigger: string, keepTrigger = false) {
  const { $from, from } = editor.state.selection;
  const before = $from.parent.textBetween(0, $from.parentOffset, '', '\ufffc');
  const index = before.lastIndexOf(trigger);
  if (index < 0) return;
  editor.commands.deleteRange({ from: from - before.length + index + (keepTrigger ? trigger.length : 0), to: from });
}
