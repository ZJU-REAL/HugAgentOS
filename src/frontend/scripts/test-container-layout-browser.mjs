
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const output=resolve('node_modules/.tmp/container-layout-browser');
await mkdir(output,{recursive:true});
await build({stdin:{contents:`
import React from 'react';
import {createRoot} from 'react-dom/client';
import {ComposerToolbar} from './src/components/chat/ComposerToolbar';
import {useModelCapabilitiesStore} from './src/stores/modelCapabilitiesStore';
import './src/styles/variables.css';
import './src/styles/common.css';
import './src/styles/chat.css';
import './src/styles/catalog.css';
import './src/styles/mcp.css';
import './src/styles/projects.css';
import './src/styles/mobile.css';
import './src/styles/capability-browser.css';
const noop=()=>{};
useModelCapabilitiesStore.setState(s=>({capabilities:{...s.capabilities,user_model_switch_enabled:true,
 user_selectable_models:[{provider_id:'long',display_name:'ExtremelyLongModelNameWithoutAnyBreaks'.repeat(4),is_default:true}]}}));
function Fixture(){
 const [plan,setPlan]=React.useState(true),[workflow,setWorkflow]=React.useState(true),[loop,setLoop]=React.useState(true);
 const [stopping,setStopping]=React.useState(false);
 const [attachOpen,setAttachOpen]=React.useState(false),[projectOpen,setProjectOpen]=React.useState(false);
 const fileRef=React.useRef(null),imageRef=React.useRef(null);
 const state={
  isCE:false,isAppAllowed:()=>true,isModeOn:()=>true,onEnterMode:noop,
  planMode:plan,workflowModeOn:workflow,loopMode:loop,setLoopMode:setLoop,showLoopEntry:true,
  onCloseMode:m=>m==='plan'?setPlan(false):setWorkflow(false),
  canReadImage:true,imageInputRef:imageRef,agents:[],skills:[],connectors:[],installedPlugins:[],
  activeLocalMode:false,setMySpaceImportOpen:noop,mySpaceImportOpen:false,
  attachOpen,setAttachOpen,fetchAgents:noop,bindChatProject:noop,unbindChatProject:noop,
  projects:[],boundProjectId:'long',boundProjectName:'LongProjectNameWithoutAnyBreaks'.repeat(10),
  currentChatId:'test',draftKey:'test',onPickProject:noop,isDesktopShell:false,
  setProjectCreateModalOpen:noop,setProjectOpen,fetchProjects:noop,projectOpen,
  setPromptHubOpen:noop,promptHubOpen:false,uploadingFiles:new Set(),
 };
 return <><section id="toolbar-host"><ComposerToolbar state={state}
  editor={{forkPending:false,sendFromComposer:()=>{document.documentElement.dataset.sent='yes';setStopping(true)},
    onPickAgentFromMenu:noop,onPickSkillFromMenu:noop,onPickConnectorFromMenu:noop,onPickPluginFromMenu:noop}}
  options={{abort:()=>{document.documentElement.dataset.stopped='yes';setStopping(false)}}}
  fileInputRef={fileRef} composerHasContent={true} showStopButton={stopping}/></section>
  <section id="cards-host">
  <div className="jx-abilityCenter"><div className="jx-agentPage"><div className="jx-agentPage-grid">
  {[1,2,3].map(i=><article key={i} className="jx-agentCard"><div className="jx-agentCard-name">{'LongAgentName'.repeat(20)}</div></article>)}
  </div></div></div>
  <div className="jx-projects"><div className="jx-projects-shell"><div className="jx-projects-grid">
  {[1,2,3].map(i=><article key={i} className="jx-projectCard"><div className="jx-projectCard-title">{'LongProjectName'.repeat(20)}</div></article>)}
  </div></div></div>
  <div className="jx-mk-grid">{[1,2,3].map(i=><article key={i} className="jx-mk-card"><div className="jx-mk-cardName">{'LongSkillName'.repeat(20)}</div></article>)}</div>
  <div className="jx-kbView"><div className="jx-kbLibraryGrid">{[1,2,3].map(i=><article key={i} className="jx-kbLibraryCard"><div className="jx-kbLibraryTitle">{'LongKnowledgeName'.repeat(20)}</div></article>)}</div></div>
  </section></>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`,resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(output,'fixture.js'),
bundle:true,format:'esm',jsx:'automatic',define:{'import.meta.env':'{}'},
external:['/loader.gif','/loader-done.png'],loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'}});
const browser=await chromium.launch({headless:true});
try {
 for(const viewport of [1440,360]) {
 for(const touch of [false,true]){
  const page=await browser.newPage({viewport:{width:viewport,height:900},hasTouch:touch});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('https://containers.test/**',async route=>{
   const path=new URL(route.request().url()).pathname;
   if(path==='/fixture.js'||path==='/fixture.css')return route.fulfill({contentType:path.endsWith('.js')?'text/javascript':'text/css',body:await readFile(resolve(output,path.slice(1)))});
   if(path.includes('tool-approval'))return route.fulfill({json:{mode:'ask'}});
   if(path.startsWith('/api'))return route.fulfill({json:{code:0,data:[]}});
   if(path.endsWith('.svg'))return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"/>'});
   return route.fulfill({contentType:'text/html',body:'<!doctype html><html><head><link rel="stylesheet" href="/fixture.css"><style>*{box-sizing:border-box}html,body,#root{height:auto;overflow:auto}body{margin:0;font:15px Arial;background:var(--color-bg-container);color:var(--color-text)}#toolbar-host,#cards-host{width:800px;margin:20px}#toolbar-host{border:1px solid var(--color-border)}#cards-host>div{height:auto;min-height:0}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
  });
  await page.goto('https://containers.test/');
  await page.locator('.jx-modeChip-close').first().waitFor();
  await page.waitForFunction(()=>[...document.querySelectorAll('.jx-modeChip')].every(el=>getComputedStyle(el).transform==='none'));
  for(const theme of ['light','dark']){
   await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
   // Let the theme color transitions settle before measuring and capturing.
   await page.waitForTimeout(200);
   for(const width of [220,240,280,320,384,400,400.5,480,500,500.5,620,620.5,800].filter(w=>w<=viewport-40)){
    await page.evaluate(({width,theme})=>{
     document.documentElement.dataset.theme=theme;
     for(const el of document.querySelectorAll('#toolbar-host,#cards-host'))el.style.width=width+'px';
    },{width,theme});
    const issues=await page.evaluate(touch=>{
     const problems=[],host=document.querySelector('#toolbar-host').getBoundingClientRect();
     const buttons=[...document.querySelectorAll('.jx-composerToolbar button')].filter(el=>el.getBoundingClientRect().width>0);
     for(const button of buttons){
      // Extremely narrow panes scroll the icon strip, while send stays fixed.
      button.scrollIntoView({block:'nearest',inline:'nearest'});
      const r=button.getBoundingClientRect(),name=button.getAttribute('aria-label')||button.title||button.className;
      if(r.left<host.left||r.right>host.right||r.bottom>host.bottom)problems.push('escaped: '+name);
      if(touch&&(r.width<43.9||r.height<43.9))problems.push('small touch target: '+name);
      const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      if(hit!==button&&!button.contains(hit))problems.push('occluded: '+name);
      const send=document.querySelector('.jx-sendBtn').getBoundingClientRect();
      if(Math.abs((r.top+r.height/2)-(send.top+send.height/2))>1)problems.push('wrapped: '+name);
      for(const other of buttons){
       if(other===button)continue;
       const b=other.getBoundingClientRect();
       const otherHit=document.elementFromPoint(b.x+b.width/2,b.y+b.height/2);
       if(otherHit!==other&&!other.contains(otherHit))continue;
       if(Math.min(r.right,b.right)-Math.max(r.left,b.left)>1&&Math.min(r.bottom,b.bottom)-Math.max(r.top,b.top)>1)problems.push('overlap: '+name);
      }
     }
     const bar=document.querySelector('.jx-composerBar');
     if(bar.hasAttribute('data-compact')){
      for(const label of bar.querySelectorAll('.jx-composerChip-label,.jx-modelEffortAside,.jx-modeArrow')){
       if(label.getBoundingClientRect().width>0)problems.push('compact label visible');
      }
      if(document.querySelector('.jx-modelEffortCompactIcon').getBoundingClientRect().width===0)problems.push('model icon missing');
     }
     for(const grid of document.querySelectorAll('#cards-host [class$="-grid"],.jx-kbLibraryGrid')){
      if(grid.scrollWidth>grid.clientWidth+1)problems.push('grid overflow: '+grid.className+' '+JSON.stringify({width:grid.clientWidth,scroll:grid.scrollWidth,columns:getComputedStyle(grid).gridTemplateColumns,children:[...grid.children].map(el=>({width:el.clientWidth,scroll:el.scrollWidth}))}));
     }
     return problems;
    },touch);
    assert.deepEqual(issues,[],JSON.stringify({width,touch,theme}));
   }
  }
  // Width recovery must restore labels without remounting.
  await page.evaluate(()=>{document.querySelector('#toolbar-host').style.width='3000px';});
  await page.waitForFunction(()=>!document.querySelector('.jx-composerBar').hasAttribute('data-compact'));
  assert.equal(await page.evaluate(()=>{
   const label=document.querySelector('.jx-modelEffortBtn .jx-composerChip-label');
   return label.clientWidth>=label.scrollWidth && getComputedStyle(label).textOverflow!=='ellipsis';
  }),true,'full model name is visible without ellipsis when space permits');
  assert.equal(await page.evaluate(()=>{
   const host=document.querySelector('#toolbar-host').getBoundingClientRect();
   return [...document.querySelectorAll('.jx-composerToolbar button')].every(el=>{
    const r=el.getBoundingClientRect(); return r.left>=host.left&&r.right<=host.right;
   });
  }),true,'expanded controls fit after labels return');
  await page.evaluate(()=>{document.querySelector('#toolbar-host').style.width='1600px';});
  await page.waitForFunction(()=>document.querySelector('.jx-composerBar').hasAttribute('data-compact'));
  assert.equal(await page.locator('.jx-modelEffortCompactIcon').isVisible(),true,'long model switches to an icon instead of truncating');
  await page.evaluate(()=>{document.querySelector('#toolbar-host').style.width='320px';});
  await page.waitForFunction(()=>document.querySelector('.jx-composerBar').hasAttribute('data-compact'));
  await page.screenshot({path:resolve(output,(touch?'touch-':'desktop-')+viewport+'.png'),fullPage:true});
  await page.evaluate(()=>{document.querySelector('#toolbar-host').style.width='240px';});
  await page.locator('.jx-attachBtn').focus();
  const tabbed=[];
  for(let i=0;i<10;i++){tabbed.push(await page.evaluate(()=>document.activeElement?.getAttribute('aria-label')));await page.keyboard.press('Tab');}
  assert.equal(new Set(tabbed.filter(label=>label?.startsWith('关闭'))).size,3);
  await page.getByRole('button',{name:'发送',exact:true}).click();
  assert.equal(await page.locator('html').getAttribute('data-sent'),'yes');
  await page.getByRole('button',{name:'中止',exact:true}).click();
  assert.equal(await page.locator('html').getAttribute('data-stopped'),'yes');
  await page.locator('.jx-modeChip-close').first().click();
  assert.equal(await page.locator('.jx-modeChip-close').count(),2);
  await page.getByRole('button',{name:'添加文件',exact:true}).click();
  await page.getByRole('menu').waitFor();
  await page.keyboard.press('Escape');
  assert.deepEqual(errors,[]);
  await page.close();
 }
 }
 console.log('Container layout: single-row icons, label restoration, width/theme/pointer cases, long names, grids, hit targets, keyboard exits, send/stop and menu passed');
}finally{await browser.close();}
