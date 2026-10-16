const status=document.querySelector('#status'),workspace=document.querySelector('#workspace');
const open=document.querySelector('#open'),close=document.querySelector('#close');
const mode=document.querySelector('#permissions'),theme=document.querySelector('#theme'),locale=document.querySelector('#locale');
let frame,port,nonce,info,active=false,lastId=0,times=[],generation=0,timeout;
const checked=await fetch('/manifest').then(r=>r.json());
const app=checked.applications[0],module=checked.modules.find(m=>m.id===app.target.moduleId);
const capabilities=['applications','workspace','host.info','workspace.navigate',...(module.grants||[])].filter(c=>['applications','workspace','host.info','workspace.navigate','data.query','file.pick','file.save'].includes(c));
const emit=body=>port?.postMessage({...body,nonce});
function dispose(message='Closed') {
 generation++;clearTimeout(timeout);active=false;emit({type:'closed'});port?.close();port=undefined;
 frame?.remove();frame=undefined;close.disabled=true;open.disabled=false;status.textContent=message;
}
function update() {
 if(!info)return;
 info={...info,theme:theme.value,locale:locale.value};
 if(active)emit({type:'update',info});
}
theme.onchange=update;locale.onchange=update;
mode.onchange=()=>dispose('Permissions changed. Open a new preview session.');
close.onclick=()=>{dispose();open.focus();};
open.onclick=async()=>{
 dispose('Loading');open.disabled=true;const epoch=generation;
 try {
  const html=await fetch('/entry').then(r=>{if(!r.ok)throw Error();return r.text();});
  if(epoch!==generation)return;
  frame=document.createElement('iframe');frame.title='Plugin example';frame.sandbox='allow-scripts';
  frame.width='100%';frame.height='500';
  frame.srcdoc='<meta http-equiv="Content-Security-Policy" content="default-src &apos;none&apos;; script-src &apos;unsafe-inline&apos;; style-src &apos;unsafe-inline&apos;; img-src data:; base-uri &apos;none&apos;; form-action &apos;none&apos;">'+html;
  workspace.append(frame);close.disabled=false;status.textContent='Waiting for SDK handshake';
  timeout=setTimeout(()=>dispose('bridge_timeout'),8000);
 } catch {dispose('Preview asset failed');}
};
window.addEventListener('message',event=>{
 if(!frame||event.source!==frame.contentWindow||event.data?.type!=='workbench:hello'||port)return;
 const hello=event.data;
 if(hello.protocolVersion!=='1.0.0'||! /^[a-f0-9]{32}$/.test(hello.clientNonce)){dispose('host_incompatible');return;}
 nonce=Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
 const channel=new MessageChannel();port=channel.port1;lastId=0;times=[];
 info={protocolVersion:'1.0.0',theme:theme.value,locale:locale.value,capabilities:mode.value==='incompatible'?['host.info']:capabilities,
  context:{applicationKey:'preview:example',installId:'preview-install',packageRevision:'preview-only'},
  route:{path:new URLSearchParams(location.search).get('view')||'/',requestId:'preview:initial'}};
 const epoch=generation;
 port.onmessage=event=>{
  const m=event.data;
  if(epoch!==generation||m?.nonce!==nonce)return;
  try {if(new TextEncoder().encode(JSON.stringify(m)).byteLength>256*1024){dispose('message_too_large');return;}}catch{return;}
  if(m.type==='ready'&&m.protocolVersion==='1.0.0'){clearTimeout(timeout);active=true;status.textContent='Ready — permission simulation';return;}
  if(m.type==='failed'){dispose('host_incompatible');return;}
  if(m.type==='dispose'){dispose('Disposed');return;}
  if(!active||m.type!=='request'||! /^[1-9][0-9]{0,15}$/.test(m.id)||Number(m.id)<=lastId)return;
  lastId=Number(m.id);
  const now=Date.now();times=times.filter(t=>now-t<10000);
  const result=(data)=>emit({type:'result',id:m.id,ok:true,data});
  const fail=code=>emit({type:'result',id:m.id,ok:false,code});
  if(times.length>=30){fail('rate_limited');return;}times.push(now);
  const p=m.params;
  if(!p||typeof p!=='object'||Array.isArray(p)){fail('invalid_params');return;}
  if(m.method==='host.info'){result(info);return;}
  if(!info.capabilities.includes(m.method)){fail('unknown_method');return;}
  if(m.method==='workspace.navigate') {
   if(typeof p.path!=='string'||(p.path!=='/'&&!(app.views||[]).some(v=>'/'+v.path.replace(/^\/+/, '')===p.path))||typeof p.requestId!=='string'||! /^[a-zA-Z0-9:_-]{1,64}$/.test(p.requestId)){fail('invalid_route');return;}
   info={...info,route:{path:p.path,requestId:p.requestId}};
   const url=new URL(location.href);url.searchParams.set('view',p.path);
   history[p.replace?'replaceState':'pushState'](null,'',url);
   result(info.route);emit({type:'update',info});return;
  }
  if(mode.value==='deny'){fail('capability_denied');return;}
  if(m.method==='data.query') {
   if(p.source!=='sample'||!module.grants.includes('data_source:sample')){fail('data_source_denied');return;}
   result({rows:[{city:'Beijing',temperature:20}],simulation:true});return;
  }
  if(m.method==='file.pick'){result({fileId:'preview-file',name:'example.txt',size:12,mimeType:'text/plain'});return;}
  if(m.method==='file.save') {
   if(p.fileId!=='preview-file'){fail('file_access_denied');return;}
   result({saved:true,simulation:true});return;
  }
  fail('unknown_method');
 };
 port.start();
 frame.contentWindow.postMessage({type:'workbench:init',nonce,clientNonce:hello.clientNonce,protocolVersion:'1.0.0',info},'*',[channel.port2]);
});
window.addEventListener('popstate',()=>{
 if(!active)return;
 info={...info,route:{path:new URLSearchParams(location.search).get('view')||'/',requestId:'preview:history:'+Date.now()}};
 emit({type:'update',info});
});
window.addEventListener('pagehide',()=>dispose());
