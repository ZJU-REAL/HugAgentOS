import {build} from 'esbuild';
import {mkdir,writeFile,copyFile} from 'node:fs/promises';
const result=await build({entryPoints:['src/main.js'],bundle:true,minify:true,write:false,format:'iife',platform:'browser',target:'es2022'});
await mkdir('dist/web',{recursive:true});
const script=result.outputFiles[0].text.replace(/<\/script/gi,'<\\/script');
const page=`<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Reports example</title><style>body{font-family:system-ui;padding:16px}body[data-theme=dark]{background:#18212b;color:#fff}button{margin:4px}pre{white-space:pre-wrap}</style>
<h1>Reports example</h1><nav aria-label="Reports views"><button id="overview">Overview</button><button id="files">Files</button></nav>
<p id="route"></p><p id="status" role="status">Connecting</p><button id="retry">Retry connection</button>
<button id="query">Query declared data source</button><button id="cancel">Cancel query</button><button id="pick">Pick file</button><button id="save" disabled>Save selected file</button>
<pre id="result" aria-live="polite"></pre><script>${script}</script></html>`;
await writeFile('dist/web/index.html',page);
await copyFile('plugin.json','dist/plugin.json');
console.log('Built independent plugin assets');
