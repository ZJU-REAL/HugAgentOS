import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
const toolsRoot = process.argv.includes('--host-tools')
  ? fileURLToPath(new URL('../../../src/frontend/', import.meta.url)) : root;
const require = createRequire(toolsRoot + 'package.json');
const ts = require('typescript');
const esbuild = require('esbuild');
for (const name of ['application', 'module', 'host']) {
  const filename = name === 'host' ? 'host.json' : name + '.schema.json';
  const schema = await readFile(root + 'schemas/' + filename, 'utf8');
  await writeFile(root + 'src/' + name + '-schema.ts', 'export default ' + schema.trim() + ' as const;\n');
}
const config = ts.readConfigFile(root + 'tsconfig.json', ts.sys.readFile);
if (config.error) throw new Error(ts.flattenDiagnosticMessageText(config.error.messageText, '\n'));
const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, root);
const program = ts.createProgram(parsed.fileNames, {
  ...parsed.options, emitDeclarationOnly: true,
  paths: {ajv: [require.resolve('ajv/lib/ajv.d.ts')]},
});
const diagnostics = [...parsed.errors, ...ts.getPreEmitDiagnostics(program)];
if (diagnostics.length) {
  throw new Error(ts.formatDiagnosticsWithColorAndContext(diagnostics, {
    getCurrentDirectory: () => root, getCanonicalFileName: n => n, getNewLine: () => '\n',
  }));
}
await mkdir(root + 'dist', {recursive: true});
if (program.emit().emitSkipped) throw new Error('Protocol declaration emit failed');
await esbuild.build({
  entryPoints: [root + 'src/index.ts', root + 'src/legacy.ts'],
  outdir: root + 'dist', bundle: true, platform: 'browser', format: 'esm', target: 'es2022',
  nodePaths: [toolsRoot + 'node_modules'],
});
