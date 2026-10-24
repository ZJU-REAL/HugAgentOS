import { scrollIntoPane, type FindSource } from '../canvasDocument';
import { findTextRanges } from './textSearch';

/** Find over whatever DOM the renderer produced (docx, markdown, code, editors). */
export function domFindSource(root: HTMLElement): FindSource {
  return {
    search: async (query) => findTextRanges(root, query).map((range) => ({
      range: () => range,
      reveal: async () => scrollIntoPane(range),
    })),
    subscribe: (listener) => {
      const observer = new MutationObserver(listener);
      observer.observe(root, { childList: true, subtree: true, characterData: true });
      return () => observer.disconnect();
    },
  };
}
