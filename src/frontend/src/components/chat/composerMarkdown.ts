import { Node as TiptapNode, nodeInputRule } from '@tiptap/core';
import { Marked, Lexer, Tokenizer, marked } from 'marked';

/**
 * The editor needs an isolated lexer: assistant rendering registers Mermaid/LaTeX
 * handlers on the global marked instance. Raw HTML in Markdown is literal here;
 * formatted HTML clipboard content is parsed separately by the editor schema.
 */
export function editorMarkdownParser(): typeof marked {
  const instance = new Marked();
  const parser = Object.assign(instance.parse.bind(instance), marked);
  parser.defaults = instance.defaults;
  parser.lexer = source => instance.lexer(source);
  parser.use = (...args) => {
    instance.use(...args);
    parser.defaults = instance.defaults;
    return parser;
  };
  parser.setOptions = options => {
    instance.setOptions(options);
    parser.defaults = instance.defaults;
    return parser;
  };
  const tokenizer = new Tokenizer({ gfm: true });
  new Lexer({ gfm: true, tokenizer });
  parser.use({
    extensions: [
      {
        name: 'literalHtmlBlock', level: 'block',
        tokenizer(source) {
          const token = tokenizer.html(source);
          return token ? { ...token, type: 'literalHtmlBlock' } : undefined;
        },
      },
      {
        name: 'literalHtmlInline', level: 'inline', start: source => source.indexOf('<'),
        tokenizer(source) {
          const token = tokenizer.tag(source);
          return token ? { ...token, type: 'literalHtmlInline' } : undefined;
        },
      },
    ],
  });
  return parser;
}

function literalNode(name: string, tokenName: string, block: boolean) {
  return TiptapNode.create({
    name,
    group: block ? 'block' : 'inline',
    inline: !block,
    atom: true,
    addAttributes() { return { raw: { default: '' } }; },
    addInputRules() {
      return tokenName === 'image' ? [nodeInputRule({
        find: /!\[[^\]]*\]\([^)]*\)$/,
        type: this.type,
        getAttributes: match => ({ raw: match[0] }),
      })] : [];
    },
    parseHTML() { return []; },
    renderHTML({ node }) {
      return block ? ['pre', {}, ['code', {}, node.attrs.raw]] : ['span', {}, node.attrs.raw];
    },
    markdownTokenName: tokenName,
    parseMarkdown(token, helpers) {
      let raw = token.raw ?? '';
      // Reference definitions are not editor nodes. Keep their resolved destination.
      if (tokenName === 'image' && !/^!\[[\s\S]*?\]\(/.test(raw) && token.href) {
        const alt = String(token.text ?? '').replace(/\]/g, '\\]');
        const href = String(token.href).replace(/>/g, '%3E');
        const title = token.title ? ' ' + JSON.stringify(token.title) : '';
        raw = '![' + alt + '](<' + href + '>' + title + ')';
      }
      return helpers.createNode(name, { raw });
    },
    renderMarkdown(node) { return node.attrs?.raw ?? ''; },
  });
}

/** Unsupported images and HTML keep their original syntax and URLs through editing. */
export const MarkdownLiterals = [
  literalNode('literalImage', 'image', false),
  literalNode('literalHtmlInline', 'literalHtmlInline', false),
  literalNode('literalHtmlBlock', 'literalHtmlBlock', true),
];
