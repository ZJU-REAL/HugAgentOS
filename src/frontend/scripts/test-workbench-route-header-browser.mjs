import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdir,readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {chromium} from 'playwright';

const output=resolve('node_modules/.tmp/workbench-route-header');
await mkdir(output,{recursive:true});
await build({stdin:{contents:`
import {createRoot} from 'react-dom/client';
import {useState} from 'react';
import {BrowserRouter,useLocation,useNavigate,useNavigationType} from 'react-router';
import {WorkspaceRouteHeader} from './src/workbench/WorkspaceRouteHeader';
function Fixture(){
 const location=useLocation(),navigate=useNavigate(),navigationType=useNavigationType();
 const path=location.pathname+location.search+location.hash;
 const active=location.pathname.startsWith('/reports');
 const host=navigationType==='POP'||!location.state?.workspaceRequestId;
 const [version,setVersion]=useState(0);
 const views=[{id:'reports',path:'/reports',title:'Reports'},{id:'details',path:'/reports/details',title:'Details'}];
 return <><button onClick={()=>{navigate('/reports/details');}}>Host details</button>
 <button onClick={()=>{navigate('/reports');}}>Host reports</button>
 <button onClick={()=>navigate('/home')}>Leave</button>
 <button onClick={()=>{navigate('/reports/details',{state:{workspaceRequestId:'plugin:detail'}});}}>Plugin details</button>
 <button onClick={()=>setVersion(v=>v+1)}>Refresh catalog</button>
 <input aria-label="Host draft"/><iframe title="Plugin input" srcDoc='<input aria-label="Plugin draft"/>'/>
 <div data-version={version}><WorkspaceRouteHeader active={active} hostNavigation={host}
 title="Example · Cloud" views={views} path={path} label="Application views"
 onNavigate={next=>navigate(next)}/></div></>;
}
createRoot(document.getElementById('root')).render(<BrowserRouter><Fixture/></BrowserRouter>);
`,resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(output,'fixture.js'),bundle:true,format:'esm',jsx:'automatic'});
const browser=await chromium.launch({headless:true});
const page=await browser.newPage();
try{
 await page.route('http://127.0.0.1:5263/**',async route=>{
  if(new URL(route.request().url()).pathname==='/fixture.js')return route.fulfill({contentType:'text/javascript',body:await readFile(resolve(output,'fixture.js'))});
  return route.fulfill({contentType:'text/html',body:'<title>Original host</title><div id="root"></div><script type="module" src="/fixture.js"></script>'});
 });
 await page.goto('http://127.0.0.1:5263/reports');
 await page.getByRole('heading',{name:'Reports · Example · Cloud'}).waitFor();
 assert.equal(await page.title(),'Reports · Example · Cloud');
 assert.equal(await page.getByRole('button',{name:'Reports',exact:true}).getAttribute('aria-current'),'page');
 await page.getByRole('button',{name:'Host details'}).click();
 const details=page.getByRole('heading',{name:'Details · Example · Cloud'});
 await details.waitFor();
 assert.equal(await page.title(),'Details · Example · Cloud');
 assert.equal(await page.getByRole('button',{name:'Details',exact:true}).getAttribute('aria-current'),'page');
 assert.equal(await details.evaluate(el=>document.activeElement===el),true);
 const draft=page.getByRole('textbox',{name:'Host draft'});
 await draft.fill('keep focus');
 await page.getByRole('button',{name:'Refresh catalog'}).evaluate(el=>el.click());
 assert.equal(await draft.evaluate(el=>document.activeElement===el),true);
 await page.getByRole('button',{name:'Host reports'}).click();
 await page.frameLocator('iframe').getByRole('textbox',{name:'Plugin draft'}).fill('inside frame');
 await page.getByRole('button',{name:'Host details'}).evaluate(el=>el.click());
 await details.waitFor();
 assert.equal(await page.locator('iframe').evaluate(el=>document.activeElement===el),true);
 await page.getByRole('button',{name:'Host reports'}).click();
 await page.getByRole('button',{name:'Plugin details'}).click();
 await details.waitFor();
 assert.equal(await details.evaluate(el=>document.activeElement===el),false);
 await page.goBack();
 await page.getByRole('heading',{name:'Reports · Example · Cloud'}).waitFor();
 assert.equal(await page.title(),'Reports · Example · Cloud');
 await page.goForward();
 await details.waitFor();
 assert.equal(await details.evaluate(el=>document.activeElement===el),true);
 await page.getByRole('button',{name:'Leave'}).click();
 await page.waitForFunction(()=>document.title==='Original host');
 console.log('Workspace route header DOM fixture passed: real URLs, back/forward, title, longest view, selected, focus, retention, iframe and plugin navigation.');
}finally{await browser.close();}
