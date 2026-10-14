import type { Editor } from '@tiptap/core';
import { DOMParser, Fragment, Slice, type Node, type Schema, type ResolvedPos } from '@tiptap/pm/model';
import DOMPurify from 'dompurify';

/** Paste uses open slices so the editor schema joins content at the selection. */
function insertClipboardSlice(editor: Editor, slice: Slice) {
  if (!slice.content.size) return;
  editor.view.dispatch(editor.state.tr
    .replaceSelection(slice)
    .setMeta('paste', true)
    .setMeta('uiEvent', 'paste')
    .scrollIntoView());
}

function isCodeSelection(editor: Editor): boolean {
  const { selection, storedMarks } = editor.state;
  return !!selection.$from.parent.type.spec.code
    || (storedMarks ?? selection.$from.marks()).some(mark => !!mark.type.spec.code);
}

/** Literal paste keeps punctuation and line breaks without parsing formatting. */
export function pastePlainText(editor: Editor, text: string) {
  if (!text) return;
  const { schema, selection, storedMarks } = editor.state;
  const marks = storedMarks ?? selection.$from.marks();
  const normalized = text.replace(/\r\n?/g, '\n');
  const content = isCodeSelection(editor)
    ? Fragment.from(schema.text(normalized, marks))
    : Fragment.fromArray(normalized.split('\n').flatMap((line, index) => [
      ...(index ? [schema.linebreakReplacement!.create()] : []),
      ...(line ? [schema.text(line, marks)] : []),
    ]));
  insertClipboardSlice(editor, new Slice(content, 0, 0));
}

/** Only ordinary text boundaries may join the surrounding paragraph. */
function openTextBoundary(node: Node | null): number {
  if (!node?.isTextblock) return 0;
  const spec = node.type.spec;
  return spec.defining || spec.definingAsContext || spec.definingForContent
    || spec.isolating || spec.code ? 0 : 1;
}

export function parseClipboardHtml(schema: Schema, html: string, context?: ResolvedPos): Fragment {
  const host = document.createElement('div');
  host.innerHTML = DOMPurify.sanitize(html, {
    FORBID_TAGS: ['style', 'script', 'iframe'], FORBID_ATTR: ['style'],
  });
  // Native browser selections add these boundary sentinels to clipboard HTML.
  // They describe the selected fragment, not a user-authored <br> in its content.
  host.querySelectorAll('br.Apple-interchange-newline').forEach(node => node.remove());
  // Remote images remain literal destinations; paste must not fetch them.
  host.querySelectorAll('img').forEach(image => {
    image.replaceWith(document.createTextNode('![' + (image.getAttribute('alt') || '')
      + '](' + (image.getAttribute('src') || '') + ')'));
  });
  return DOMParser.fromSchema(schema).parseSlice(host, {
    // Use HTML whitespace semantics. Global preservation converts layout LFs
    // into hardBreak nodes; <br>, paragraphs and code own their whitespace.
    context,
  }).content;
}

/** HTML and Markdown parsers both feed the same native paste operation. */
export function insertClipboardContent(editor: Editor, data: Pick<DataTransfer, 'getData'>) {
  const text = data.getData('text/plain');
  if (isCodeSelection(editor)) {
    pastePlainText(editor, text);
    return;
  }
  const html = data.getData('text/html');
  if (!html && !text) return;
  const content = html
    ? parseClipboardHtml(editor.schema, html, editor.state.selection.$from)
    : editor.schema.nodeFromJSON(editor.markdown!.parse(text)).content;
  insertClipboardSlice(editor, new Slice(content,
    openTextBoundary(content.firstChild), openTextBoundary(content.lastChild)));
}
