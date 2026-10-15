/** Browser component evidence only; final installed-plugin HTTP E2E is separate. */
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdir,writeFile,readFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {resolve} from 'node:path';
import {chromium} from 'playwright';
const scratch=resolve('../../.scratch/workbench-10/browser');
await mkdir(scratch,{recursive:true});
await build({stdin:{contents:`
import {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {WorkspaceInstances} from './src/workbench/lifecycle/WorkspaceInstances';
function Fixture(){
 const [active,setActive]=useState(null),[owner,setOwner]=useState('alice'),[revoked,setRevoked]=useState([]);
 const [loads,setLoads]=useState(0),[closes,setCloses]=useState(0);
 const identity=key=>({userId:owner,applicationKey:key,installId:'exact-install',packageRevision:'r1',executionScope:'cloud',origin:'fixture-origin',workspace:'main'});
 return <>
  <output id="loads">{loads}</output><output id="closes">{closes}</output>
  {['a','b','c','d','e',...Array.from({length:100},(_,i)=>'catalog-'+i)].map(key=>
   <button key={key} onClick={()=>setActive({identity:identity(key)})}>{'open '+key}</button>)}
  <button onClick={()=>setOwner('bob')}>switch user</button>
  <button onClick={()=>setRevoked(['a','b','c','d','e'])}>revoke</button>
  <WorkspaceInstances active={active} ownerScope={owner}
   load={async()=>{setLoads(n=>n+1);return 'page';}}
   validate={entry=>entry.identity.userId===owner&&!revoked.includes(entry.identity.applicationKey)}
   onDispose={()=>setCloses(n=>n+1)}
   onBlocked={current=>setActive(current?{identity:current}:null)}
   render={(entry,extension,actions)=><>
    <button onClick={()=>extension.handlers['lifecycle.setDirty']({dirty:true},entry.signal)}>{'dirty '+entry.identity.applicationKey}</button>
    <button onClick={()=>void actions.close().then(ok=>{if(ok)setActive(null);})}>{'close '+entry.identity.applicationKey}</button>
    <iframe title={entry.identity.applicationKey} srcDoc="<input aria-label='Draft'>" onLoad={actions.ready}/>
   </>}/>
 </>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`,resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(scratch,'fixture.js'),bundle:true,
 format:'esm',jsx:'automatic',loader:{'.css':'empty'},define:{'import.meta.env':'{}'}});
await writeFile(resolve(scratch,'index.html'),'<div id="root"></div><script type="module" src="/fixture.js"></script>');
const server=createServer(async(req,res)=>{
 try{
  const asset=req.url==='/fixture.js'?'fixture.js':'index.html';
  res.setHeader('Content-Type',asset.endsWith('.js')?'text/javascript':'text/html');
  res.end(await readFile(resolve(scratch,asset)));
 }catch{res.statusCode=500;res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true});
const page=await browser.newPage();
const errors=[];page.on('pageerror',error=>errors.push(error.message));
try{
 await page.goto('http://127.0.0.1:'+server.address().port);
 assert.equal(await page.locator('iframe').count(),0,'Catalog must not start iframe');
 const open=async key=>{await page.getByRole('button',{name:'open '+key,exact:true}).click();await page.locator('iframe[title="'+key+'"]').waitFor({state:'visible'});};
 await open('a');
 await page.frameLocator('iframe[title="a"]').getByLabel('Draft').fill('unsaved draft');
 await open('b');await open('a');
 assert.equal(await page.frameLocator('iframe[title="a"]').getByLabel('Draft').inputValue(),'unsaved draft');
 assert.equal(await page.locator('#loads').textContent(),'2','Reopen must reuse instance');
 await page.getByRole('button',{name:'dirty a',exact:true}).click();
 page.once('dialog',dialog=>dialog.dismiss());
 await page.getByRole('button',{name:'open c',exact:true}).click();
 assert.equal(await page.locator('iframe[title="a"]').isVisible(),true);
 assert.equal(await page.locator('#loads').textContent(),'2');
 page.once('dialog',dialog=>dialog.accept());
 await open('c');await open('d');await open('e');
 assert.equal(await page.locator('iframe').count(),3);
 assert.equal(await page.locator('iframe[title="a"]').count(),0);
 assert.equal(await page.locator('iframe[title="b"]').count(),0);
 await page.getByRole('button',{name:'dirty e',exact:true}).click();
 await page.getByRole('button',{name:'revoke',exact:true}).click();
 await page.waitForFunction(()=>document.querySelectorAll('iframe').length===0);
 assert.equal(errors.length,0,errors.join('\n'));
 await page.screenshot({path:resolve(scratch,'retained-instances.png'),fullPage:true});
 console.log('WB10 browser component: lazy open, retained draft, dirty cancel/discard, bounded LRU and forced revoke passed');
}finally{
 await browser.close();await new Promise(resolve=>server.close(resolve));
}
