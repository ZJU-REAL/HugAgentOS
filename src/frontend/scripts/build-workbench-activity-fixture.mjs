import {build} from 'esbuild';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
const root = resolve('../..');
const out = resolve(root, '.scratch/workbench-18/browser-package');
await mkdir(resolve(out,'web'), {recursive: true});
const declaration = {schemaVersion: '1.0.0', id: 'activity-test', title: 'Activity test',
  target: {kind: 'plugin', moduleId: 'page'}, permissions: ['badge','notification','background']};
const manifest = {name: 'workbench-activity-test', version: '1.0.0',
  extensions: {'org.hugagent': {ui: {version: 3, contributes: {
    applications: [declaration], modules: [{id: 'page', surface: 'workspace', entry: 'web/index.html',
      title: 'Activity test', grants: ['badge.update','notification.show','background']}],
  }}}}};
await writeFile(resolve(out,'plugin.json'), JSON.stringify(manifest));
await writeFile(resolve(out,'web/index.html'), '<h1>Activity test</h1><button id="publish" disabled>Publish activity</button><p role="status"></p><script type="module" src="page.js"></script>');
await build({stdin: {contents:
  "import {connect,updateBadge,showNotification} from '@hugagent/workbench-sdk';" +
  "const c=await connect();const button=document.getElementById('publish');button.disabled=false;" +
  "button.onclick=async()=>{try{await updateBadge(c,7);await showNotification(c,{title:'Test notice',body:'Plain text notification'});" +
  "document.querySelector('p').textContent='Published';}catch(e){document.querySelector('p').textContent=e.message}};",
  resolveDir: process.cwd(), loader: 'js'}, bundle: true, format: 'esm', target: 'es2022',
  alias: {'@hugagent/workbench-sdk': resolve(root,'packages/workbench-sdk/dist/index.js')},
  outfile: resolve(out,'web/page.js')});
console.log(out + ' — zip these package contents and import only into the isolated test account.');
