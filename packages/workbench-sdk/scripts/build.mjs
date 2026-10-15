import {createRequire} from 'node:module';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const hostTools = process.argv.includes('--host-tools');
const toolsRoot = hostTools ? fileURLToPath(new URL('../../../src/frontend/', import.meta.url)) : root;
const require = createRequire(toolsRoot + 'package.json');
const ts = require('typescript');
const config = ts.readConfigFile(root + 'tsconfig.json', ts.sys.readFile);
if (config.error) throw new Error(ts.flattenDiagnosticMessageText(config.error.messageText, '\n'));
const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, root);
const protocol = resolve(root, '../workbench-protocol');
const options = hostTools ? {...parsed.options, paths: {
  '@hugagent/workbench-protocol': [protocol + '/dist/index.d.ts'],
  '@hugagent/workbench-protocol/*': [protocol + '/*'],
}} : parsed.options;
const compilerHost = ts.createCompilerHost(options);
if (hostTools) {
  // Match npm's linked-package boundary: protocol JSON belongs to the dependency,
  // not the SDK's declaration output tree.
  compilerHost.resolveModuleNames = (names, containingFile) => names.map(name => {
    const resolved = ts.resolveModuleName(name, containingFile, options, compilerHost).resolvedModule;
    return resolved && name.startsWith('@hugagent/workbench-protocol')
      ? {...resolved, isExternalLibraryImport: true} : resolved;
  });
}
const program = ts.createProgram(parsed.fileNames, options, compilerHost);
const diagnostics = [...parsed.errors, ...ts.getPreEmitDiagnostics(program)];
if (diagnostics.length) throw new Error(ts.formatDiagnosticsWithColorAndContext(diagnostics, {
  getCurrentDirectory: () => root, getCanonicalFileName: name => name, getNewLine: () => '\n',
}));
const emitted = program.emit();
if (emitted.emitSkipped) throw new Error('SDK declaration emit failed: ' +
  ts.formatDiagnosticsWithColorAndContext(emitted.diagnostics, {
    getCurrentDirectory: () => root, getCanonicalFileName: name => name, getNewLine: () => '\n',
  }));
await require('esbuild').build({
  entryPoints: [root + 'src/index.ts'], outfile: root + 'dist/index.js',
  bundle: true, platform: 'browser', format: 'esm', target: 'es2022',
  ...(hostTools ? {alias: {'@hugagent/workbench-protocol': protocol},
    nodePaths: [toolsRoot + 'node_modules']} : {}),
});

await require('esbuild').build({entryPoints:[root+'tooling/cli.mjs'],outfile:root+'dist/tooling/cli.mjs',
 bundle:true,platform:'node',format:'esm',target:'node20',
 alias:{'@hugagent/workbench-protocol':root+'../workbench-protocol/dist/index.js'}});
await require('esbuild').build({entryPoints:[root+'tooling/preview.mjs'],outfile:root+'dist/tooling/preview.mjs',
 bundle:true,platform:'browser',format:'esm',target:'es2022'});
