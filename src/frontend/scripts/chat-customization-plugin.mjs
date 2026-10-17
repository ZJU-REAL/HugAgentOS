import { existsSync } from 'node:fs';
import path from 'node:path';

const virtualId = 'virtual:hugagent-chat-customization';
/** Only trusted build configuration selects executable customer code. Never a URL parameter. */
export function chatCustomizationPlugin({ entry, root }) {
  let resolved = null;
  if (entry && (entry.startsWith('.') || path.isAbsolute(entry))) {
    resolved = path.resolve(root, entry);
    if (!existsSync(resolved)) throw new Error('Chat customization entry does not exist: ' + resolved);
  }
  return {
    name: 'hugagent-chat-customization',
    enforce: 'pre',
    resolveId(id, importer) {
      if (id === virtualId || (id === './selected' && importer?.endsWith('/customization/ChatCustomization.tsx'))) return '\0' + virtualId;
    },
    async load(id) {
      if (id !== '\0' + virtualId) return null;
      if (entry && !resolved) {
        const found = await this.resolve(entry, path.join(root, 'package.json'), { skipSelf: true });
        if (!found || found.external) this.error('Cannot resolve chat customization package: ' + entry);
        resolved = found.id;
      }
      return resolved ? 'export { default } from ' + JSON.stringify(resolved) + ';' : 'export default null;';
    },
  };
}
