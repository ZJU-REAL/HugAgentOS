import {createServer} from 'node:http';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {readManifest,readAsset} from './manifest.mjs';
const page=`<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Workbench permission preview</title>
<h1>Workbench permission preview</h1><p>Simulation only. No production account, backend, file access or native bridge.</p>
<label>Permissions <select id="permissions"><option value="allow">Allow declared example capabilities</option><option value="deny">Deny data and file capabilities</option><option value="incompatible">Missing required capability</option></select></label>
<label>Theme <select id="theme"><option>light</option><option>dark</option></select></label>
<label>Language <select id="locale"><option>en</option><option>zh-CN</option></select></label>
<button id="open">Open example</button><button id="close" disabled>Close</button>
<p id="status" role="status">Unopened: no iframe</p><main id="workspace"></main>
<script type="module" src="/preview.mjs"></script></html>`;
export async function startPreview(root,port=4179) {
 const checked=readManifest(join(root,'plugin.json'),{assets:root});
 if(!checked.valid)throw Error('invalid_manifest');
 const module=checked.contributions.modules.find(m=>m.id===checked.contributions.applications[0].target.moduleId);
 if(!module)throw Error('missing_module');
 const entry=readAsset(root,module.entry);
 const publicContributions={applications:checked.contributions.applications,modules:checked.contributions.modules.map(({id,grants})=>({id,grants}))};
 const script=readFileSync(new URL('./preview.mjs',import.meta.url),'utf8');
 const server=createServer((request,response)=>{
  const allowedHost='127.0.0.1:'+server.address().port;
  if(request.headers.host!==allowedHost||request.method!=='GET') {response.writeHead(403);response.end();return;}
  let url;try {url=new URL(request.url,'http://'+allowedHost);}catch {response.writeHead(400);response.end();return;}
  const routes={
   '/': ['text/html; charset=utf-8',page],
   '/preview.mjs':['text/javascript; charset=utf-8',script],
   '/manifest':['application/json',JSON.stringify(publicContributions)],
   '/entry':['text/plain; charset=utf-8',entry],
  };
  const route=routes[url.pathname];
  response.setHeader('Cache-Control','no-store');
  response.setHeader('X-Content-Type-Options','nosniff');
  response.setHeader('Referrer-Policy','no-referrer');
  response.setHeader('Content-Security-Policy',"default-src 'none'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'self' about:; style-src 'self'; frame-ancestors 'none'; base-uri 'none'");
  if(!route){response.writeHead(404);response.end();return;}
  response.writeHead(200,{'Content-Type':route[0]});response.end(route[1]);
 });
 await new Promise((ok,fail)=>{server.once('error',fail);server.listen(port,'127.0.0.1',ok);});
 console.log('PREVIEW_SIMULATION http://127.0.0.1:'+server.address().port);
 return server;
}
