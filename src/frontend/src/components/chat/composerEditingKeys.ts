import type { Editor } from '@tiptap/core';

/** Shift+Enter uses structural editing where Markdown blocks need continuation. */
export function hasStructuralEnter(editor: Editor | null | undefined): boolean {
  return !!editor && ['listItem', 'taskItem', 'blockquote', 'heading', 'table', 'codeBlock']
    .some(node => editor.isActive(node));
}

/** Pipe tables can be authored with Enter, without pasting or a separate source mode. */
export function handleTableEnter(editor: Editor): boolean {
  const { $from } = editor.state.selection;
  if ($from.depth !== 1 || $from.parent.type.name !== 'paragraph') return false;
  const line = $from.parent.textContent;
  if (!/^\s*\|/.test(line)) return false;
  const serialize = (node: typeof $from.parent) => editor.markdown!.serialize({ type: 'doc', content: [node.toJSON()] });
  const containsChip = (node: typeof $from.parent) => {
    let found = false;
    node.descendants(child => { if (child.type.name === 'invocationChip') found = true; });
    return found;
  };
  if (containsChip($from.parent)) { editor.commands.splitBlock(); return true; }
  const lines = [serialize($from.parent)];
  let from = $from.before();
  for (let index = $from.index(0) - 1; index >= 0; index--) {
    const previous = editor.state.doc.child(index);
    if (previous.type.name !== 'paragraph' || !/^\s*\|/.test(previous.textContent)) break;
    if (containsChip(previous)) { editor.commands.splitBlock(); return true; }
    lines.unshift(serialize(previous));
    from -= previous.nodeSize;
  }
  const source = lines.join('\n');
  const parsed = editor.markdown?.parse(source);
  if (parsed?.content?.some(node => node.type === 'table')) {
    editor.chain().insertContentAt({ from, to: $from.after() }, source, { contentType: 'markdown' }).focus().run();
  } else {
    editor.commands.splitBlock();
  }
  return true;
}

/** Sending owns Enter; Shift+Enter reuses native block splitting without a DOM key event. */
export function handleComposerNewline(editor: Editor): void {
  if (handleTableEnter(editor)) return;
  if (hasStructuralEnter(editor)) editor.commands.keyboardShortcut('Enter');
  else editor.commands.setHardBreak();
}
