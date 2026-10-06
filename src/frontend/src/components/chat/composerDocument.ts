import type { Editor, JSONContent } from '@tiptap/core';
import type { ComposerDraft } from '../../stores/composerStore';
import type { ChatCommand } from '../../utils/projectCommands';
import type { ReferencableChat } from '../../types';
import { insertChipAtStart, removeChipsOfType } from './composerEditorDom';

const CAPABILITIES = [
  ['mention', 'activeMention', '@'], ['skill', 'activeSkill', '/'],
  ['plugin', 'activePlugin', '/'], ['connector', 'activeConnector', 'MCP'],
] as const;

/** Text, rich structure and invocation references are one atomic editor snapshot. */
export function snapshotComposer(editor: Editor): Partial<ComposerDraft> {
  const values = new Map<string, { id: string; name: string }>();
  const referencedChats: ReferencableChat[] = [];
  let activeCommand: ChatCommand | null = null;
  editor.state.doc.descendants(node => {
    if (node.type.name !== 'invocationChip' || !node.attrs.value) return;
    const value = JSON.parse(node.attrs.value);
    if (node.attrs.chip === 'chat') referencedChats.push(value);
    else if (node.attrs.chip === 'command') activeCommand = value;
    else values.set(node.attrs.chip, value);
  });
  return {
    input: editor.getMarkdown(), document: editor.getJSON(),
    ...Object.fromEntries(CAPABILITIES.map(([type, field]) => [field, values.get(type) ?? null])),
    activeCommand, referencedChats,
  };
}

/** Restore stored structure; external prompt/chip changes use the same document reconciliation. */
export function restoreComposer(editor: Editor, draft: ComposerDraft) {
  if (draft.document) editor.chain().setContent(draft.document, { emitUpdate: false }).setMeta('addToHistory', false).run();
  else editor.chain().setContent(draft.input, { contentType: 'markdown', emitUpdate: false }).setMeta('addToHistory', false).run();
  const element = editor.view.dom as HTMLElement;
  const stored = snapshotComposer(editor);
  const reconcile = (type: string, current: unknown, target: unknown, insert: () => void) => {
    if (JSON.stringify(current) === JSON.stringify(target)) return;
    removeChipsOfType(element, type);
    insert();
  };
  for (const [type, field, prefix] of CAPABILITIES) {
    const value = draft[field];
    reconcile(type, stored[field], value, () => {
      if (value) insertChipAtStart(element, prefix, value.name, 'jx-editorChip--' + type, type, value);
    });
  }
  reconcile('command', stored.activeCommand, draft.activeCommand, () => {
    if (draft.activeCommand) insertChipAtStart(element, '/', draft.activeCommand.label, 'jx-editorChip--command', 'command', draft.activeCommand);
  });
  reconcile('chat', stored.referencedChats, draft.referencedChats, () => {
    for (const chat of [...draft.referencedChats].reverse()) insertChipAtStart(element, '', chat.title, 'jx-editorChip--chat', 'chat', chat);
  });
}
export function sameDocument(a: JSONContent | null, b: JSONContent | null): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}
