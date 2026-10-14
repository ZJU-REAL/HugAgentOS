import { InputRule, Node as TiptapNode, mergeAttributes, renderNestedMarkdownContent } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import { Heading } from '@tiptap/extension-heading';
import { Paragraph } from '@tiptap/extension-paragraph';
import { CodeBlock } from '@tiptap/extension-code-block';
import { Code } from '@tiptap/extension-code';
import { Fragment } from '@tiptap/pm/model';
import { Link } from '@tiptap/extension-link';
import { Markdown } from '@tiptap/markdown';
import { TaskList, TaskItem, OrderedList } from '@tiptap/extension-list';
import { Table, TableKit } from '@tiptap/extension-table';
import DOMPurify from 'dompurify';
import { editorMarkdownParser, MarkdownLiterals } from './composerMarkdown';

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

// Inline code owns its literal text; surrounding HTML uses normal whitespace.
const ComposerCode = Code.extend({
  parseHTML() {
    return [{ tag: 'code', getContent: (element, schema) => {
      const read = (node: globalThis.Node): string =>
        node.nodeType === 3 ? node.textContent ?? ''
          : node.nodeName === 'BR' ? '\n' : Array.from(node.childNodes).map(read).join('');
      const text = read(element);
      if (!text) return Fragment.empty;
      if (element.parentElement?.closest('pre')) return Fragment.from(schema.text(text));
      return Fragment.fromArray(text.split('\n').flatMap((line, index) => [
        ...(index ? [schema.nodes.hardBreak.create()] : []),
        ...(line ? [schema.text(line)] : []),
      ]));
    } }];
  },
});

const ComposerTaskList = TaskList.extend({ content: '(taskItem | listItem)+' });
const ComposerOrderedList = OrderedList.extend({ content: '(listItem | taskItem)+' });

const MarkdownTaskItem = TaskItem.extend({
  renderMarkdown(node, helpers, context) {
    // Checkboxes are task state, not part of the Markdown list indentation column.
    const marker = context.parentType === 'orderedList'
      ? String((context.meta?.parentAttrs?.start ?? 1) + context.index) + '. ' : '- ';
    const content = renderNestedMarkdownContent(node, helpers, marker, context,
      { alignNestedToPrefix: context.parentType === 'orderedList' });
    return marker + '[' + (node.attrs?.checked ? 'x' : ' ') + '] ' + content.slice(marker.length);
  },
  addInputRules() {
    return [
      new InputRule({
        find: /^\s*\[([ xX])\]\s$/,
        handler: ({ range, match, chain }) => {
          if (!this.editor.isActive('listItem')) return null;
          chain().deleteRange(range).toggleTaskList().command(({ tr }) => {
            // Mixed lists accept ordinary items. A typed checkbox marker explicitly
            // changes this item to a task; changing the container alone is insufficient.
            for (let depth = tr.selection.$from.depth; depth > 0; depth -= 1) {
              if (tr.selection.$from.node(depth).type.name !== 'listItem') continue;
              tr.setNodeMarkup(tr.selection.$from.before(depth), this.type,
                { checked: match[1].toLowerCase() === 'x' });
              break;
            }
            return true;
          }).updateAttributes('taskItem', { checked: match[1].toLowerCase() === 'x' }).run();
        },
      }),
      ...(this.parent?.() ?? []),
    ];
  },
});

function escapeBlockMarkers(text: string): string {
  return text.replace(/^([ \t]{0,3})(#{1,6}|>|[-+])(?=\s|$)/gm, '$1\\$2')
    .replace(/^([ \t]{0,3}\d+)([.)])(?=\s)/gm, '$1\\$2')
    .replace(/^([ \t]{0,3})(=+|-+)(?=[ \t]*$)/gm, '$1\\$2');
}

// Authored editor text owns its whitespace; arbitrary HTML keeps layout semantics.
const ComposerParagraph = Paragraph.extend({
  parseHTML() {
    return [{ tag: 'p[data-whitespace="preserve"]', preserveWhitespace: 'full' },
      ...(this.parent?.() ?? [])];
  },
  renderHTML({ HTMLAttributes }) {
    return ['p', mergeAttributes(HTMLAttributes, { 'data-whitespace': 'preserve' }), 0];
  },
  renderMarkdown(node, helpers, context) {
    if (!node.content?.length) return this.parent?.(node, helpers, context) ?? '';
    let output = '';
    let inline: NonNullable<typeof node.content> = [];
    const flush = () => {
      output += escapeBlockMarkers(helpers.renderChildren(inline));
      inline = [];
    };
    for (let index = 0; index < node.content.length; index += 1) {
      const child = node.content[index];
      if (child.type === 'hardBreak' && node.content[index + 1]?.type === 'hardBreak') {
        let count = 1;
        while (node.content[index + count]?.type === 'hardBreak') count += 1;
        // Consecutive breaks form Markdown paragraph spacing. A hard-break
        // delimiter before a blank line would become literal trailing spaces.
        flush();
        output += '\n'.repeat(count);
        index += count - 1;
      } else inline.push(child);
    }
    flush();
    return output;
  },
});

const ComposerHeading = Heading.extend({
  renderMarkdown(node, helpers, context) {
    const text = helpers.renderChildren(node.content ?? []);
    const level = node.attrs?.level ?? 1;
    // Setext headings can contain several lines; ATX headings cannot.
    if (level <= 2 && text.includes('\n')) {
      return escapeBlockMarkers(text) + '\n' + (level === 1 ? '===' : '---');
    }
    return this.parent?.(node, helpers, context) ?? '';
  },
});

const ComposerCodeBlock = CodeBlock.extend({
  renderMarkdown(node, helpers) {
    const text = helpers.renderChildren(node.content ?? []);
    const longest = (text.match(/`+/g) ?? []).reduce((length, run) => Math.max(length, run.length), 0);
    const fence = '`'.repeat(Math.max(3, longest + 1));
    return fence + (node.attrs?.language ?? '') + '\n' + text + '\n' + fence;
  },
});

// Table serialization owns its rows. The document serializer owns block separators.
const ComposerTable = Table.extend({
  renderMarkdown(node, helpers, context) {
    return (this.parent?.(node, helpers, context) ?? '').replace(/^\n|\n$/g, '');
  },
});

export function composerExtensions() {
  return [
    StarterKit.configure({ underline: false, trailingNode: false, code: false, orderedList: false, paragraph: false, heading: false, codeBlock: false,
      link: { openOnClick: false, markdownLinks: true, linkOnPaste: false,
        shouldAutoLink: url => (!url.includes('@') || /^[a-z][a-z0-9+.-]*:\/\//i.test(url))
          && Link.options.shouldAutoLink(url) } }),
    ComposerCode, ComposerParagraph, ComposerHeading, ComposerCodeBlock.configure({ enableTabIndentation: true }), TableKit.configure({ table: false }), ComposerTable, ComposerTaskList, ComposerOrderedList, MarkdownTaskItem.configure({ nested: true }), InvocationChip,
    ...MarkdownLiterals,
    Markdown.configure({ marked: editorMarkdownParser(), markedOptions: { gfm: true, breaks: true } }),
  ];
}
