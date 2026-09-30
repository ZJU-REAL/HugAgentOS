import { build } from 'esbuild';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

// Exercise the real Zustand store; replace only its browser/API boundaries.
globalThis.projectTestApi = {};
const apiNames = ['createProject', 'deleteProject', 'listProjects',
  'removeProjectFile', 'toggleProjectFavorite', 'updateProject', 'uploadProjectFile'];
const outfile = resolve('node_modules/.tmp/test-project-instructions.mjs');
await build({
  entryPoints: ['scripts/test-project-instructions.ts'], bundle: true, platform: 'node',
  format: 'esm', outfile, define: { 'import.meta.env': '{}' },
  plugins: [{
    name: 'project-browser-boundaries',
    setup(build) {
      build.onResolve({ filter: /^\.\.\/api$|^\.\.\/editionApi$|^\.\.\/storage$|^\.\/chatStore$|^\.\.\/i18n$|^\.\.\/routing\/navigation$/ }, (args) => {
        if (args.importer.endsWith('/stores/projectStore.ts')) return { path: args.path, namespace: 'test-boundary' };
      });
      build.onLoad({ filter: /.*/, namespace: 'test-boundary' }, (args) => ({
        contents: args.path === '../api'
          ? `export const getProject=(...a)=>globalThis.projectTestApi.read(...a);
             export const updateProjectInstructions=(...a)=>globalThis.projectTestApi.write(...a);
             export const listProjectChats=(...a)=>globalThis.projectTestApi.chats(...a);
             export const listProjectFiles=(...a)=>globalThis.projectTestApi.files(...a);
             ${apiNames.map(n => `export const ${n}=()=>{throw new Error('Unexpected API call: ${n}');};`).join('\n')}`
          : args.path === '../routing/navigation' ? 'export const navigateTo=()=>{}; export const pathForPanel=()=>"/projects/p1";'
          : args.path === '../editionApi' ? 'export const listMyTeamsForProjects=async()=>[];'
          : args.path === '../storage' ? 'export const registerUnboundProject=()=>{};'
          : args.path === './chatStore' ? 'export const useChatStore={getState:()=>({})};'
          : 'export const t=(value)=>value;',
        loader: 'js',
      }));
    },
  }],
});
await import(pathToFileURL(outfile).href);
