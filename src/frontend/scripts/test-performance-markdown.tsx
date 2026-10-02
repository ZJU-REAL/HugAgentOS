import assert from 'node:assert/strict';
import React from 'react';
import { renderToString } from 'react-dom/server';
import { marked } from 'marked';
import CitationMarkdownBlock from '../src/components/citation/CitationMarkdownBlock';
let parses = 0;
marked.use({ hooks: { postprocess(html: string) { parses += 1; return html; } } });
renderToString(<CitationMarkdownBlock text={'**Result** [source](cite:e1)'} isMarkdown citations={[{id:'e1', title:'Source', snippet:'Evidence', url:'https://example.com', tool_name:'internet_search'}]} />);
assert.equal(parses, 1, 'a cited Markdown update must parse exactly once');
parses = 0;
const plain = renderToString(<CitationMarkdownBlock text={'**Result**'} isMarkdown citations={[]} />);
assert.ok(plain.includes('<strong>Result</strong>'));
assert.equal(parses, 1);
console.log('Markdown single-pass rendering passed');


const { default: highlighter, requestLanguage } = await import('../src/utils/syntaxHighlighting');
assert.ok(highlighter.listLanguages().length < 50, 'only common grammars are eager');
requestLanguage('clojure');
for (let i=0; i<100 && !highlighter.getLanguage('clojure'); i++) {
  await new Promise(resolve => setTimeout(resolve, 10));
}
assert.ok(highlighter.getLanguage('clojure'), 'uncommon grammar loads on demand');
assert.match(highlighter.highlight('(def answer 42)', { language:'clojure' }).value, /hljs-/);
console.log('Common grammar bundle and lazy uncommon grammar fallback passed');
