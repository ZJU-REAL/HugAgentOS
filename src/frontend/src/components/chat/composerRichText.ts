import { Editor, InputRule, Node as TiptapNode, mergeAttributes } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import { Markdown } from '@tiptap/markdown';
import { TaskList, TaskItem } from '@tiptap/extension-list';
import { TableKit } from '@tiptap/extension-table';
import DOMPurify from 'dompurify';
import { closeHistory } from '@tiptap/pm/history';
import { editorMarkdownParser, MarkdownLiterals } from './composerMarkdown';
import { DOMSerializer } from '@tiptap/pm/model';
import { insertClipboardContent } from './composerClipboard';

const editors = new WeakMap<HTMLElement, Editor>();
export function richEditor(element: HTMLElement): Editor | undefined { return editors.get(element); }

const InvocationChip = TiptapNode.create({
  name: 'invocationChip', inline: true, group: 'inline', atom: true, selectable: true,
  addAttributes() {
    return Object.fromEntries(['chip', 'chipName', 'chipId', 'prefix', 'class', 'value', 'prefixIcon'].map(key => [key, { default: '' }]));
  },
  parseHTML() { return []; }, // External clipboard HTML cannot activate a capability.
  renderHTML({ node, HTMLAttributes }) {
    const icon = document.createElement('span');
    icon.className = 'jx-editorChip-prefix';
    if (node.attrs.prefixIcon) icon.innerHTML = DOMPurify.sanitize(node.attrs.prefixIcon);
    else icon.textContent = node.attrs.prefix;
    // Capability metadata is internal editor state, never clipboard HTML.
    const visibleAttributes = { ...HTMLAttributes };
    delete visibleAttributes.value;
    delete visibleAttributes.prefixIcon;
    return ['span', mergeAttributes(visibleAttributes, {
      'data-chip': node.attrs.chip, 'data-chip-name': node.attrs.chipName, 'data-chip-id': node.attrs.chipId,
      contenteditable: 'false', class: 'jx-editorChip ' + node.attrs.class,
    }), icon,
    ['span', { class: 'jx-editorChip-name' }, node.attrs.chipName]];
  },
  renderMarkdown() { return ''; },
});

const MarkdownTaskItem = TaskItem.extend({
  addInputRules() {
    return [
      new InputRule({
        find: /^\s*\[([ xX])\]\s$/,
        handler: ({ range, match, chain }) => {
          if (!this.editor.isActive('listItem')) return null;
          chain().deleteRange(range).toggleTaskList()
            .updateAttributes('taskItem', { checked: match[1].toLowerCase() === 'x' }).run();
        },
      }),
      ...(this.parent?.() ?? []),
    ];
  },
});

export function createRichEditor(element: HTMLElement, onUpdate: () => void): Editor {
  const editor = new Editor({
    element,
    // Clipboard parsing owns formatting; running paste rules again would double-parse it.
    enablePasteRules: false,
    extensions: [
      StarterKit.configure({ underline: false, codeBlock: { enableTabIndentation: true }, link: { openOnClick: false, markdownLinks: true } }),
      TableKit, TaskList, MarkdownTaskItem.configure({ nested: true }), InvocationChip, ...MarkdownLiterals, Markdown.configure({ marked: editorMarkdownParser(), markedOptions: { gfm: true, breaks: true } }),
    ],
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
