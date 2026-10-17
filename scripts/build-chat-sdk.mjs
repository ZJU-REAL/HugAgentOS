import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = fileURLToPath(new URL('../', import.meta.url));
const frontend = path.join(root, 'src/frontend');
const require = createRequire(path.join(frontend, 'package.json'));
const ts = require('typescript');
const config = path.join(root, 'packages/chat-sdk/tsconfig.json');
const loaded = ts.readConfigFile(config, ts.sys.readFile);
if (loaded.error) throw new Error(ts.flattenDiagnosticMessageText(loaded.error.messageText, '\n'));
const parsed = ts.parseJsonConfigFileContent(loaded.config, ts.sys, path.dirname(config));
const options = { ...parsed.options, baseUrl: frontend, paths: {
  react: ['node_modules/@types/react/index.d.ts'],
  'react/jsx-runtime': ['node_modules/@types/react/jsx-runtime.d.ts'],
} };
const program = ts.createProgram(parsed.fileNames, options);
const emit = program.emit();
const errors = [...parsed.errors, ...ts.getPreEmitDiagnostics(program), ...emit.diagnostics];
if (errors.length) {
  console.error(ts.formatDiagnosticsWithColorAndContext(errors, {
    getCurrentDirectory: ts.sys.getCurrentDirectory, getCanonicalFileName: f => f, getNewLine: () => '\n',
  }));
  process.exit(1);
}
