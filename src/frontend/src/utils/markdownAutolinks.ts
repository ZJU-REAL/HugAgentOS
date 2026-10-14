import { Tokenizer, type MarkedExtension } from 'marked';

/** Bare email-shaped text is ambiguous in commands; only explicit mail links are links. */
export const markdownAutolinks: MarkedExtension['tokenizer'] = {
  url(source) {
    const token = Tokenizer.prototype.url.call(this, source);
    return token?.href.startsWith('mailto:')
      ? undefined
      : token;
  },
};
